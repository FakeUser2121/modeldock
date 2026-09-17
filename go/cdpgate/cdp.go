package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"sync"
	"syscall"
	"time"

	"github.com/chromedp/chromedp"
	"github.com/chromedp/cdproto/page"
	"github.com/chromedp/cdproto/target"
)

// session = the Chromium child process + a chromedp remote allocator
// attached to the CDP websocket that process opened. The sidecar owns the
// process lifecycle (spawn/supervise/kill); chromedp is only the command
// layer over the already-known websocket URL. The chromedp context stays
// open for the life of the session so one process serves many tool calls.
type session struct {
	cmd         *exec.Cmd
	wsURL       string
	port        string
	targetID    string
	url         string
	ctx         context.Context
	cancel      context.CancelFunc
	allocCancel context.CancelFunc
	target      *chromedp.Target
	profileDir  string
	eventsLog   *os.File
	logMu       sync.Mutex
	frameMu     sync.Mutex
	framePath   string
	lastFrame   time.Time
}

// logEvent appends every CDP event reaching the attached target to
// <profileDir>/events.log. Diagnostic aid for the screencast pipeline:
// if Page.screencastFrame events appear here, chromedp delivery works and
// the gap is in the writer/ack path; if they do not appear, the gap is on
// the Chromium side (compositor/BeginFrame/occlusion).

// attachRemote connects chromedp to the running browser.
//
// chromedp.NewRemoteAllocator returns a bare allocator context; chromedp.Run
// refuses it ("invalid context") unless it is wrapped with chromedp.NewContext
// (which is what supplies the cancel func Run requires). We attach to the
// browser's existing page target (found via the plain-HTTP /json endpoint)
// instead of letting chromedp open a second about:blank tab: one window, one
// tab, the URL already loaded.
func (s *session) attachRemote(wsURL string, port string) error {
	s.port = port
	id, err := findPageTarget(port, 15*time.Second)
	if err != nil {
		return fmt.Errorf("find page target: %w", err)
	}
	s.targetID = id

	allocCtx, allocCancel := chromedp.NewRemoteAllocator(context.Background(), wsURL)
	s.allocCancel = allocCancel
	ctx, cancel := chromedp.NewContext(allocCtx, chromedp.WithTargetID(target.ID(id)))
	s.ctx = ctx
	s.cancel = cancel
	if err := chromedp.Run(ctx); err != nil {
		cancel()
		allocCancel()
		return fmt.Errorf("attach: %w", err)
	}
	// Target only exists after Run; grab it here.
	s.target = chromedp.FromContext(ctx).Target
	return nil
}

// findPageTarget polls the CDP HTTP endpoint /json until a "page" target
// exists (the browser websocket can be up before the first tab is ready).
func findPageTarget(port string, timeout time.Duration) (string, error) {
	deadline := time.Now().Add(timeout)
	client := &http.Client{Timeout: 2 * time.Second}
	for {
		var targets []map[string]any
		resp, err := client.Get("http://127.0.0.1:" + port + "/json")
		if err == nil {
			_ = json.NewDecoder(resp.Body).Decode(&targets)
			resp.Body.Close()
			for _, t := range targets {
				if tt, _ := t["type"].(string); tt == "page" {
					if id, _ := t["id"].(string); id != "" {
						return id, nil
					}
				}
			}
		}
		if time.Now().After(deadline) {
			return "", fmt.Errorf("no page target on port %s within %s", port, timeout)
		}
		time.Sleep(250 * time.Millisecond)
	}
}

// evalJS runs an expression in the attached page and returns its value.
func (s *session) evalJS(js string) (any, error) {
	var res map[string]any
	err := chromedp.Run(s.ctx, chromedp.ActionFunc(func(ctx context.Context) error {
		c := chromedp.FromContext(ctx)
		params := map[string]any{"expression": js, "returnByValue": true, "awaitPromise": true}
		return c.Target.Execute(ctx, "Runtime.evaluate", params, &res)
	}))
	if err != nil { return nil, err }
	if e, ok := res["exceptionDetails"].(map[string]any); ok {
		ex, _ := e["exception"].(map[string]any)
		msg, _ := ex["description"].(string)
		return "EXCEPTION: " + msg, nil
	}
	if r, ok := res["result"].(map[string]any); ok {
		if t, _ := r["type"].(string); t == "undefined" {
			return "undefined", nil
		}
		return r["value"], nil
	}
	return res, nil
}

// navigate navigates the attached page to url.
func (s *session) navigate(url string) error {
	var res map[string]any
	err := chromedp.Run(s.ctx, chromedp.ActionFunc(func(ctx context.Context) error {
		c := chromedp.FromContext(ctx)
		params := map[string]any{"url": url}
		return c.Target.Execute(ctx, "Page.navigate", params, &res)
	}))
	if err != nil { return err }
	if et, ok := res["errorText"].(string); ok && et != "" {
		return fmt.Errorf("navigate: %s", et)
	}
	s.url = url
	return nil
}

// screenshot captures the attached page as PNG and writes it to path.
func (s *session) screenshot(path string) error {
	var res map[string]any
	err := chromedp.Run(s.ctx, chromedp.ActionFunc(func(ctx context.Context) error {
		c := chromedp.FromContext(ctx)
		return c.Target.Execute(ctx, "Page.captureScreenshot", map[string]any{"format": "png"}, &res)
	}))
	if err != nil { return err }
	data, _ := res["data"].(string)
	b, err := base64.StdEncoding.DecodeString(data)
	if err != nil { return err }
	return os.WriteFile(path, b, 0o644)
}

// startScreencast begins streaming page frames (JPEG) to path. Frames are
// decimated (everyNthFrame) and written at most once per second, so the
// sidecar stays cheap while the pane is closed; the pane polls the file.
// Page.screencastFrameAck must be sent per frame or the stream stalls.
//
// Chromium refuses Page.startScreencast with -32000 ("Not attached to an
// active page") while the page is mid-navigation (RenderFrameHost not
// kActive). We retry with backoff so a navigate→screencast sequence works.
func (s *session) startScreencast(path string) error {
	s.frameMu.Lock()
	defer s.frameMu.Unlock()
	if s.framePath != "" {
		return nil
	}
	s.framePath = path
	frames := make(chan *page.EventScreencastFrame, 8)
	go func() {
		for e := range frames {
			if b, err := base64.StdEncoding.DecodeString(e.Data); err == nil {
				s.writeFrameThrottled(b)
			}
			_ = s.target.Execute(s.ctx, "Page.screencastFrameAck", map[string]any{"sessionId": e.SessionID}, nil)
		}
	}()
	chromedp.ListenTarget(s.ctx, func(ev any) {
		s.logEvent(ev)
		if e, ok := ev.(*page.EventScreencastFrame); ok {
			select {
			case frames <- e:
			default:
			}
		}
	})
	params := map[string]any{"format": "jpeg", "quality": 50, "maxWidth": 640, "maxHeight": 360, "everyNthFrame": 2}
	var lastErr error
	for attempt := 0; attempt < 5; attempt++ {
		if attempt > 0 {
			time.Sleep(250 * time.Millisecond)
		}
		lastErr = s.target.Execute(s.ctx, "Page.startScreencast", params, nil)
		if lastErr == nil {
			return nil
		}
	}
	return lastErr
}

func (s *session) logEvent(ev any) {
	if s.eventsLog == nil {
		return
	}
	size := 0
	if e, ok := ev.(*page.EventScreencastFrame); ok {
		size = len(e.Data)
	}
	line := fmt.Sprintf("t=%.3f %T size=%d\n", float64(time.Now().UnixNano())/1e9, ev, size)
	s.logMu.Lock()
	_, _ = s.eventsLog.WriteString(line)
	s.logMu.Unlock()
}

// writeFrameThrottled writes a frame at most once per second.
func (s *session) writeFrameThrottled(b []byte) {
	for time.Since(s.lastFrame) < time.Second {
		time.Sleep(100 * time.Millisecond)
	}
	if err := os.WriteFile(s.framePath, b, 0o644); err == nil {
		s.lastFrame = time.Now()
	}
}

// stopScreencast stops frame streaming; the listener keeps running but idle.
func (s *session) stopScreencast() error {
	s.frameMu.Lock()
	defer s.frameMu.Unlock()
	if s.framePath == "" {
		return nil
	}
	s.framePath = ""
	return s.target.Execute(s.ctx, "Page.stopScreencast", nil, nil)
}

// status reports process + connection state.
func (s *session) status() map[string]any {
	return map[string]any{
		"started": true,
		"pid":     s.cmd.Process.Pid,
		"url":     s.url,
		"target":  s.targetID,
	}
}

// close detaches chromedp (graceful target close + ws close) and signals the
// Chromium process to exit.
func (s *session) close() {
	if s.cancel != nil { s.cancel() }
	if s.allocCancel != nil { s.allocCancel() }
	if s.cmd != nil && s.cmd.Process != nil {
		_ = s.cmd.Process.Signal(syscall.SIGTERM)
	}
}
