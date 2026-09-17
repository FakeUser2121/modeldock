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

func spawnChromium(profile, url string) (*session, error) {
	if err := os.MkdirAll(profile, 0o755); err != nil { return nil, err }
	// never trust a stale DevToolsActivePort from a previous run
	_ = os.Remove(profile + "/DevToolsActivePort")
	cmd := exec.Command("chromium",
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
		"--ozone-platform=wayland",
		"--user-data-dir="+profile,
		"--window-size=1280,720",
		"--remote-debugging-port=0",
		"--no-first-run",
		url,
	)
	logf, _ := os.Create(profile + "/chromium.log")
	if logf != nil { cmd.Stderr = logf }
	if err := cmd.Start(); err != nil { return nil, err }
	s := &session{cmd: cmd, profileDir: profile}
	s.eventsLog, _ = os.OpenFile(profile+"/events.log", os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	dta, err := waitForDevToolsPort(profile, 15*time.Second)
	if err != nil { s.close(); return nil, err }
	s.wsURL = "ws://127.0.0.1:" + dta.port + dta.path
	if err := s.attachRemote(s.wsURL, dta.port); err != nil { s.close(); return nil, err }
	return s, nil
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
