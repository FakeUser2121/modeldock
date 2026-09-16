"""Minimal OpenAI-compatible mock server for testing modeldock.

Run:  python tests/mock_server.py [port]   (default 8901)

- GET  /v1/models            -> two mock models
- POST /v1/chat/completions  -> streaming reply; if the last user message
  contains:
    "USE_TOOL"       -> tool_call `echo hello-from-mock` (inside the workspace)
    "USE_OUTSIDE"    -> tool_call `cat /etc/passwd` (leaves the workspace -> approval)
    "READONLY_DENY"  -> tool_call `rm -rf notes.txt` (write op -> denied in readonly)
    "USE_JSON_TOOL"  -> plain text containing the JSON fallback marker line
    "USE_MCP"        -> tool_call `mcp__mockmcp__echo` (MCP routing test)
    "USE_EXTEND"     -> tool_call `extend_harness` registering a `greet` command tool
    "USE_CUSTOM"     -> tool_call `greet` (custom tool; keyed off its own tool_call_id,
                        so it fires even after an earlier tool result)
    "TOOLS_400"      -> HTTP 400 mentioning "tools" (no-tools retry test)
    "TOOL_ERROR"     -> HTTP 400 (adapter error test)
  Once a `tool`-role result has been fed back, the mock answers with plain
  text so the tool loop terminates (except USE_CUSTOM, which is keyed off the
  matching tool_call_id in the history).
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

MODELS = ["mock-model-a", "mock-model-b"]


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

    def _sse(self, obj):
        self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
        self.wfile.flush()

    def do_GET(self):
        if self.path == "/v1/models":
            self._send_json(200, {"data": [{"id": m, "object": "model"} for m in MODELS]})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "bad json"})
            return
        if self.path != "/v1/chat/completions":
            self._send_json(404, {"error": "not found"})
            return

        last_user = ""
        has_tool_result = False
        for m in req.get("messages", []):
            if m.get("role") == "user":
                last_user = m.get("content", "")
            if m.get("role") == "tool":
                has_tool_result = True

        # extend/custom triggers key off their own tool_call_id in the history,
        # because the custom-tool call may follow an earlier (already-answered)
        # tool result in the same session.
        answered_ext = any(
            m.get("role") == "tool" and m.get("tool_call_id") == "call_mock_ext"
            for m in req.get("messages", [])
        )
        answered_greet = any(
            m.get("role") == "tool" and m.get("tool_call_id") == "call_mock_greet"
            for m in req.get("messages", [])
        )

        has_tools = bool(req.get("tools"))

        # "TOOLS_400": refuse with HTTP 400 only while tools are in the
        # request; after the harness retries without tools, answer normally
        # so the no-tools fallback path is exercised end-to-end.
        if "TOOLS_400" in last_user and has_tools:
            self._send_json(400, {"error": "tools are not supported by this model"})
            return
        if "TOOL_ERROR" in last_user:
            self._send_json(400, {"error": "tools not supported by this mock"})
            return

        def tool_call_obj(cmd, reason, call_id):
            return [
                {
                    "index": 0,
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "run_command",
                        "arguments": json.dumps({"cmd": cmd, "reason": reason}),
                    },
                }
            ]

        stream = req.get("stream", False)
        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            json_marker_reply = None
            tc = None
            if not answered_greet and "USE_CUSTOM" in last_user:
                tc = [
                    {
                        "index": 0,
                        "id": "call_mock_greet",
                        "type": "function",
                        "function": {
                            "name": "greet",
                            "arguments": json.dumps({"who": "world"}),
                        },
                    }
                ]
            elif not answered_ext and "USE_EXTEND" in last_user:
                tc = [
                    {
                        "index": 0,
                        "id": "call_mock_ext",
                        "type": "function",
                        "function": {
                            "name": "extend_harness",
                            "arguments": json.dumps(
                                {
                                    "name": "greet",
                                    "kind": "command",
                                    "template": "echo hello {who}",
                                    "params": ["who"],
                                    "description": "Greets someone",
                                }
                            ),
                        },
                    }
                ]
            elif not has_tool_result and "USE_MCP_STDIO" in last_user:
                tc = [
                    {
                        "index": 0,
                        "id": "call_mock_stdio",
                        "type": "function",
                        "function": {
                            "name": "mcp__mockstdio__add",
                            "arguments": json.dumps({"a": 2, "b": 40}),
                        },
                    }
                ]
            elif not has_tool_result and "USE_MCP" in last_user:
                tc = [
                    {
                        "index": 0,
                        "id": "call_mock_mcp",
                        "type": "function",
                        "function": {
                            "name": "mcp__mockmcp__echo",
                            "arguments": json.dumps({"msg": "hello from mcp"}),
                        },
                    }
                ]
            elif not has_tool_result and "USE_JSON_TOOL" in last_user:
                json_marker_reply = (
                    "Sure, I will run that via the marker protocol:\n"
                    "[[tool:{\"cmd\": \"echo json-fallback-works\", \"reason\": \"testing the JSON fallback\"}]]\n"
                    "That command should have run."
                )
            elif not has_tool_result and "USE_OUTSIDE" in last_user:
                tc = tool_call_obj("cat /etc/passwd", "mock wants to read a file outside the workspace", "call_mock_out")
            elif not has_tool_result and "READONLY_DENY" in last_user:
                tc = tool_call_obj("rm -rf notes.txt", "mock wants to delete a file", "call_mock_deny")
            elif not has_tool_result and "USE_TOOL" in last_user:
                tc = tool_call_obj("echo hello-from-mock", "mock wants a demo run", "call_mock_1")
            else:
                tc = None
            if tc is not None:
                self._sse({"choices": [{"delta": {"tool_calls": tc}, "finish_reason": "tool_calls"}]})
            else:
                reply = json_marker_reply or f"mock reply to: {last_user[:80]}"
                for word in reply.split(" "):
                    self._sse({"choices": [{"delta": {"content": word + " "}, "finish_reason": None}]})
                self._sse({"choices": [{"delta": {}, "finish_reason": "stop"}]})
                self._sse({"usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            self._send_json(
                200,
                {
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": f"mock reply to: {last_user[:80]}"},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8901
    print(f"mock server on 127.0.0.1:{port}")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
