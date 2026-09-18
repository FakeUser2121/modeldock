package main

import (
	"fmt"
	"os"
	"os/exec"
	"strings"
	"time"
)

type devtoolsActive struct {
	port string
	path string
}

// browserBinary picks the Chromium executable: MODELDOCK_CHROMIUM wins,
// otherwise the first name found on PATH. Hardcoding "chromium" failed
// outright on distros that ship it as "chromium-browser" or only have Chrome.
func browserBinary() string {
	if env := os.Getenv("MODELDOCK_CHROMIUM"); env != "" {
		return env
	}
	for _, name := range []string{"chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome"} {
		if p, err := exec.LookPath(name); err == nil {
			return p
		}
	}
	return "chromium"
}

// ozonePlatform: a hardcoded --ozone-platform=wayland makes Chromium fail to
// start on an X11 session. Honour an override, else infer from the session.
func ozonePlatform() string {
	if env := os.Getenv("MODELDOCK_OZONE_PLATFORM"); env != "" {
		return env
	}
	if os.Getenv("WAYLAND_DISPLAY") != "" {
		return "wayland"
	}
	if os.Getenv("DISPLAY") != "" {
		return "x11"
	}
	return "auto"
}

func windowSize() string {
	if env := os.Getenv("MODELDOCK_VIEWPORT"); env != "" {
		return strings.ReplaceAll(env, "x", ",")
	}
	return "1280,720"
}

func spawnChromium(profile, url string) (*session, error) {
	if err := os.MkdirAll(profile, 0o755); err != nil { return nil, err }
	// never trust a stale DevToolsActivePort from a previous run
	_ = os.Remove(profile + "/DevToolsActivePort")
	cmd := exec.Command(browserBinary(),
		// HEADFUL: no --headless flag anywhere. Software GL (SwiftShader) is
		// independent of headful/headless — we need a real visible window.
		// Chromium 153 maps --use-gl=angle --use-angle=swiftshader to
		// ANGLE-on-SwiftShader (ui/gl/gl_implementation.cc kGLImplementationNamePairs
		// has no bare "swiftshader" entry; --use-gl=swiftshader is invalid and makes
		// GL init fail). --ignore-gpu-blocklist = "Override software rendering list".
		"--use-gl=angle",
		"--use-angle=swiftshader",
		"--enable-unsafe-swiftshader",
		"--ignore-gpu-blocklist",
		"--ozone-platform="+ozonePlatform(),
		"--user-data-dir="+profile,
		"--window-size="+windowSize(),
		"--remote-debugging-port=0",
		"--no-first-run",
		url,
	)
	logf, _ := os.Create(profile + "/chromium.log")
	if logf != nil { cmd.Stderr = logf }
	if err := cmd.Start(); err != nil {
		if logf != nil { _ = logf.Close() }
		return nil, err
	}
	s := &session{cmd: cmd, profileDir: profile, chromeLog: logf}
	s.eventsLog, _ = os.OpenFile(profile+"/events.log", os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	dta, err := waitForDevToolsPort(profile, 15*time.Second)
	if err != nil { s.abort(); return nil, err }
	s.wsURL = "ws://127.0.0.1:" + dta.port + dta.path
	if err := s.attachRemote(s.wsURL, dta.port); err != nil { s.abort(); return nil, err }
	return s, nil
}

// abort tears down a half-started session: signal, then reap. The old path
// called close() and returned, leaving the Chromium child unwaited-for —
// a zombie for the lifetime of the sidecar.
func (s *session) abort() {
	s.close()
	if s.cmd == nil || s.cmd.Process == nil {
		return
	}
	done := make(chan struct{})
	go func() { _ = s.cmd.Wait(); close(done) }()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		_ = s.cmd.Process.Kill()
		_ = s.cmd.Wait()
	}
}

func waitForDevToolsPort(profile string, timeout time.Duration) (devtoolsActive, error) {
	f := profile + "/DevToolsActivePort"
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if b, err := os.ReadFile(f); err == nil {
			lines := strings.Split(strings.TrimSpace(string(b)), "\n")
			if len(lines) >= 2 && lines[0] != "" {
				return devtoolsActive{port: lines[0], path: strings.TrimSpace(lines[1])}, nil
			}
		}
		time.Sleep(200 * time.Millisecond)
	}
	return devtoolsActive{}, fmt.Errorf("DevToolsActivePort not ready within %s (profile %s)", timeout, profile)
}
