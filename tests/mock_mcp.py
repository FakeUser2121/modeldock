"""Minimal streamable-HTTP MCP server for testing modeldock.

Run:  python tests/mock_mcp.py [port]   (default 8902)

POST /mcp accepts JSON-RPC:
  initialize   -> serverInfo + capabilities
  tools/list   -> two tools: echo, get_time
  tools/call   -> echo returns {"msg": ...}; get_time returns the current time

Responds with plain application/json (a valid streamable-HTTP response).
"""
import datetime
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

SERVER_ID = "mockmcp"

TOOLS = [
    {
        "name": "echo",
        "description": "Echo back the given message.",
        "inputSchema": {
            "type": "object",
            "properties": {"msg": {"type": "string", "description": "Message to echo"}},
            "required": ["msg"],
        },
    },
    {
        "name": "get_time",
        "description": "Return the current UTC time.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet
        pass

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _rpc_reply(self, rid, result):
        self._send_json(200, {"jsonrpc": "2.0", "id": rid, "result": result})

    def _rpc_error(self, rid, code, message):
        self._send_json(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})

    def do_POST(self):
        if self.path != "/mcp":
            self._send_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "bad json"})
            return
        rid = req.get("id")
        method = req.get("method", "")
        params = req.get("params") or {}

        if method == "initialize":
            self._rpc_reply(
                rid,
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_ID, "version": "0.1.0"},
                },
            )
        elif method == "tools/list":
            self._rpc_reply(rid, {"tools": TOOLS})
        elif method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments") or {}
            if name == "echo":
                text = f"echo: {args.get('msg', '')}"
            elif name == "get_time":
                text = datetime.datetime.now(datetime.timezone.utc).isoformat()
            else:
                self._rpc_reply(rid, {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True})
                return
            self._rpc_reply(rid, {"content": [{"type": "text", "text": text}], "isError": False})
        elif method == "notifications/initialized" or method.startswith("notifications/"):
            self._send_json(202, {})
        else:
            self._rpc_error(rid, -32601, f"method not found: {method}")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8902
    print(f"mock MCP server on 127.0.0.1:{port}")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
