// mdock-cdp — ModelDock CDP sidecar (Go layer: process + async/network).
//
// Modes:
//
//	one-shot: mdock-cdp <profileDir> <url> <jsExpr> [screenshotPath]
//
//	serve:    mdock-cdp --serve <profileDir>
//
// --serve reads one JSON command per line on stdin and writes one JSON
// response per line on stdout (Stage 2 protocol; one persistent process
// per chat, spawned/supervised by the Python harness):
//
//	{"id":"...","cmd":"open","url":"..."}
//	{"id":"...","cmd":"navigate","url":"..."}
//	{"id":"...","cmd":"eval","js":"..."}
//	{"id":"...","cmd":"screenshot","path":"..."}
//	{"id":"...","cmd":"screencast_start","path":"..."}
//	{"id":"...","cmd":"screencast_stop"}
//	{"id":"...","cmd":"status"}
//	{"id":"...","cmd":"close"}
//
// Responses:
//
//	{"id":"...","ok":true,"value":...}
//	{"id":"...","ok":false,"error":"..."}
package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"time"
)

type command struct {
	ID   string `json:"id,omitempty"`
	Cmd  string `json:"cmd"`
	URL  string `json:"url,omitempty"`
	JS   string `json:"js,omitempty"`
	Path string `json:"path,omitempty"`
}

type response struct {
	ID    string          `json:"id,omitempty"`
	OK    bool            `json:"ok"`
	Value json.RawMessage `json:"value,omitempty"`
	Error string          `json:"error,omitempty"`
}

func reply(id string, ok bool, value any, errMsg string) {
	r := response{ID: id, OK: ok, Error: errMsg}
	if ok && value != nil {
		if b, err := json.Marshal(value); err == nil {
			r.Value = b
		}
	}
	b, _ := json.Marshal(r)
	fmt.Println(string(b))
}

// shutdown detaches chromedp, SIGTERMs chromium, waits up to 10s, then kills.
func shutdown(s *session) {
	s.close()
	done := make(chan struct{})
	go func() { _ = s.cmd.Wait(); close(done) }()
	select {
	case <-done:
	case <-time.After(10 * time.Second):
		_ = s.cmd.Process.Kill()
	}
}

func main() {
	if len(os.Args) >= 2 && os.Args[1] == "--serve" {
		if len(os.Args) < 3 {
			fmt.Fprintln(os.Stderr, "usage: mdock-cdp --serve <profileDir>")
			os.Exit(2)
		}
		serve(os.Args[2])
		return
	}
	if len(os.Args) < 4 {
		fmt.Fprintln(os.Stderr, "usage: mdock-cdp <profileDir> <url> <jsExpr> [screenshotPath]")
		os.Exit(2)
	}
	profile, url, js := os.Args[1], os.Args[2], os.Args[3]
	shot := ""
	if len(os.Args) > 4 { shot = os.Args[4] }

	s, err := spawnChromium(profile, url)
	if err != nil { fmt.Fprintln(os.Stderr, "spawn:", err); os.Exit(1) }
	defer shutdown(s)

	res, err := s.evalJS(js)
	if err != nil { fmt.Fprintln(os.Stderr, "eval:", err); os.Exit(1) }
	b, _ := json.Marshal(res)
	fmt.Println(string(b))

	if shot != "" {
		if err := s.screenshot(shot); err != nil { fmt.Fprintln(os.Stderr, "screenshot:", err); os.Exit(1) }
		fmt.Fprintln(os.Stderr, "screenshot ->", shot)
	}
}

// serve runs the JSONL command loop until "close" or stdin EOF.
func serve(profile string) {
	var s *session
	sc := bufio.NewScanner(os.Stdin)
	sc.Buffer(make([]byte, 1024*1024), 1024*1024)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" {
			continue
		}
		var c command
		if err := json.Unmarshal([]byte(line), &c); err != nil {
			reply("", false, nil, "bad command: "+err.Error())
			continue
		}
		switch c.Cmd {
		case "open":
			if s != nil {
				reply(c.ID, true, s.status(), "")
				continue
			}
			if c.URL == "" {
				reply(c.ID, false, nil, "open requires url")
				continue
			}
			sp, err := spawnChromium(profile, c.URL)
			if err != nil {
				reply(c.ID, false, nil, "spawn: "+err.Error())
				continue
			}
			s = sp
			reply(c.ID, true, s.status(), "")
		case "navigate":
			if s == nil {
				reply(c.ID, false, nil, "browser not started")
				continue
			}
			if err := s.navigate(c.URL); err != nil {
				reply(c.ID, false, nil, err.Error())
				continue
			}
			reply(c.ID, true, s.status(), "")
		case "eval":
			if s == nil {
				reply(c.ID, false, nil, "browser not started")
				continue
			}
			res, err := s.evalJS(c.JS)
			if err != nil {
				reply(c.ID, false, nil, err.Error())
				continue
			}
			reply(c.ID, true, res, "")
		case "screenshot":
			if s == nil {
				reply(c.ID, false, nil, "browser not started")
				continue
			}
			if err := s.screenshot(c.Path); err != nil {
				reply(c.ID, false, nil, err.Error())
				continue
			}
			reply(c.ID, true, map[string]string{"path": c.Path}, "")
		case "screencast_start":
			if s == nil {
				reply(c.ID, false, nil, "browser not started")
				continue
			}
			if c.Path == "" {
				reply(c.ID, false, nil, "screencast_start requires path")
				continue
			}
			if err := s.startScreencast(c.Path); err != nil {
				reply(c.ID, false, nil, err.Error())
				continue
			}
			reply(c.ID, true, map[string]string{"path": c.Path}, "")
		case "screencast_stop":
			if s == nil {
				reply(c.ID, false, nil, "browser not started")
				continue
			}
			if err := s.stopScreencast(); err != nil {
				reply(c.ID, false, nil, err.Error())
				continue
			}
			reply(c.ID, true, map[string]any{"stopped": true}, "")
		case "status":
			if s == nil {
				reply(c.ID, true, map[string]any{"started": false}, "")
				continue
			}
			reply(c.ID, true, s.status(), "")
		case "close":
			if s == nil {
				reply(c.ID, true, map[string]any{"started": false}, "")
				return
			}
			shutdown(s)
			reply(c.ID, true, map[string]any{"started": false}, "")
			return
		default:
			reply(c.ID, false, nil, "unknown cmd: "+c.Cmd)
		}
	}
	// stdin EOF: clean teardown of the sidecar.
	if s != nil {
		shutdown(s)
	}
}