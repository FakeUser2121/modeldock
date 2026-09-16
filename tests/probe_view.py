#!/usr/bin/env python3
"""Open a probe page in a real WebKit window (same engine as gui.py).
The probe POSTs its measurement JSON to 127.0.0.1:8891 (no-cors).

Usage: probe_view.py [URL]  (default http://127.0.0.1:8890/_probe.html)
"""
import os
import sys

os.environ.setdefault("GSETTINGS_BACKEND", "memory")
os.environ.setdefault("WEBKIT_DISABLE_COMPOSITING_MODE", "1")

import webview

url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8890/_probe.html"
webview.create_window("probe", url, width=1180, height=760)
webview.start()
