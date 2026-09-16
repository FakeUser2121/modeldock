"""Minimal stdio MCP server for testing modeldock.

Run:  python tests/mock_mcp_stdio.py

Reads newline-delimited JSON-RPC on stdin, writes JSON-RPC responses on
stdout. Supports initialize / tools/list / tools/call (tool: add).
"""
import datetime
import json
import sys

TOOLS = [
    {
        "name": "add",
        "description": "Add two numbers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "number"},
                "b": {"type": "number"},
            },
            "required": ["a", "b"],
        },
    },
]


def reply(rid, result):
    print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}), flush=True)


def error(rid, message):
    print(json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": message}}), flush=True)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid = req.get("id")
        method = req.get("method", "")
        params = req.get("params") or {}
        if method == "initialize":
            reply(rid, {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mock-stdio", "version": "0.1.0"},
            })
        elif method == "tools/list":
            reply(rid, {"tools": TOOLS})
        elif method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments") or {}
            if name == "add":
                try:
                    total = float(args.get("a", 0)) + float(args.get("b", 0))
                    text = f"{total:g}"
                except (TypeError, ValueError):
                    reply(rid, {"content": [{"type": "text", "text": "bad numbers"}], "isError": True})
                    continue
                reply(rid, {"content": [{"type": "text", "text": text}], "isError": False})
            else:
                reply(rid, {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True})
        elif method.startswith("notifications/"):
            continue
        else:
            error(rid, f"method not found: {method}")


if __name__ == "__main__":
    main()
