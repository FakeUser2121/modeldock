#!/usr/bin/env python3
"""Tiny result collector: POST body -> ../modeldock/.probe-result.json"""
import http.server
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        (ROOT / ".probe-result.json").write_bytes(self.rfile.read(n))
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


http.server.HTTPServer(("127.0.0.1", 8891), H).serve_forever()
