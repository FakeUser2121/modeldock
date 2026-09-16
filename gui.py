#!/usr/bin/env python3
"""ModelDock standalone — the web UI in its own native window.

One process, two halves:
  * the modeldock FastAPI backend, running in-process (uvicorn on
    loopback, default 127.0.0.1:8787)
  * a WebKit (GTK) webview window that loads the exact same web UI —
    web/index.html + css + js — from that in-process backend

No browser: the window is the app. The UI files are the real UI,
unmodified; this is just a standalone instance hosting them. All
traffic stays on loopback (one local HTTP connection inside the
window).

Requires: a display (DISPLAY) and system WebKit2GTK + GTK3.

Run:
    uv run python gui.py                  # standalone window, port 8787
    MODELDOCK_PORT=8800 uv run python gui.py
"""
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

# Keep GTK/dconf away from the read-only /run/user/<uid> (sandbox surface) and
# keep WebKit on the CPU compositing path (the local GPU driver is unreliable):
os.environ.setdefault("GSETTINGS_BACKEND", "memory")
os.environ.setdefault("WEBKIT_DISABLE_COMPOSITING_MODE", "1")

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402
import webview  # noqa: E402
from harness.main import app  # noqa: E402

HOST = os.environ.get("MODELDOCK_HOST", "127.0.0.1")
PORT = int(os.environ.get("MODELDOCK_PORT", "8787"))
URL = f"http://{HOST}:{PORT}/"


def start_backend() -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    return server


def wait_ready(server: uvicorn.Server, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if server.should_exit:
            raise RuntimeError(f"backend on {HOST}:{PORT} exited at startup")
        try:
            with urllib.request.urlopen(f"{URL}api/health", timeout=2):
                return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError(f"backend did not come up on {URL}")


def main() -> None:
    if not os.environ.get("DISPLAY"):
        raise SystemExit(
            "no DISPLAY set — the standalone window needs an X display "
            "(use 'uv run python run.py' for the headless server instead)"
        )
    try:
        server = start_backend()
        wait_ready(server)
    except Exception as e:
        raise SystemExit(f"backend failed to start on {HOST}:{PORT}: {e}")

    webview.create_window(
        "ModelDock",
        URL,
        width=1180,
        height=760,
        min_size=(720, 480),
        text_select=True,
        resizable=True,
        background_color="#1e1f24",
    )
    webview.start()  # runs the window loop; returns when the window closes
    # process exit tears down the daemon backend thread with it


if __name__ == "__main__":
    main()
