"""End-to-end test for the extend mode + custom tools (harness extensions).

Requires the mock OpenAI server on 8901 and modeldock on 8787.

Flow:
  1. extend session: "USE_EXTEND" -> model registers a `greet` command tool
     via the extend_harness meta tool.
  2. same session: "USE_CUSTOM" -> model calls the registered `greet` tool;
     output must be "hello world".
  3. every event is recorded in the ledgers (harness_extended, custom_tool_call).
  4. user API CRUD: add / toggle / delete a tool through /api/tools.
  5. negative: a write-mode session cannot use extend_harness.
  6. negative: a disabled custom tool is refused.
  7. readonly still obeys the sandbox: a write command is denied, but a
     transparent read command through a custom tool is allowed.
"""
import json
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8787"
WS = "/home/user3/Documents/unsafe/modeldock/.test-ws"
# The server re-reads data/extensions.json on every operation, so deleting
# it makes this suite idempotent across runs.
EXT_FILE = Path(__file__).resolve().parent.parent / "data" / "extensions.json"


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(r) as resp:
        return json.loads(resp.read().decode())


def sse_chat(sid, message, on_event):
    """Run one chat turn; collect events via on_event. Returns event list."""
    seen = []
    data = json.dumps({"message": message}).encode()
    r = urllib.request.Request(f"{BASE}/api/sessions/{sid}/chat", data=data,
        method="POST", headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(r)
    try:
        buf = b""
        while True:
            chunk = resp.read(1)
            if not chunk:
                break
            buf += chunk
            if buf.endswith(b"\n\n"):
                line = buf.decode().strip()
                if line.startswith("data: "):
                    payload = line[6:]
                    if payload == "[DONE]":
                        break
                    ev = json.loads(payload)
                    seen.append(ev)
                    on_event(ev)
                buf = b""
    finally:
        resp.close()
    return seen


def make_session(title, mode):
    s = req("POST", "/api/sessions", {"title": title, "workspace": WS})
    sid = s["id"]
    servers = req("GET", "/api/servers").get("servers", [])
    mock = next(s for s in servers if s.get("url", "").startswith("http://127.0.0.1:8901"))
    req("PUT", f"/api/sessions/{sid}",
        {"server": mock["id"], "model": "mock-model-a", "mode": mode})
    return sid


def tools():
    return req("GET", "/api/tools").get("tools", [])


def decisions(path_query):
    d = req("GET", "/api/decisions" + path_query)
    return d.get("entries", [])


def main():
    EXT_FILE.unlink(missing_ok=True)

    # ---------- 1. extend turn: model registers `greet` ----------
    sid = make_session("extend-smoke", "extend")
    seen = sse_chat(sid, "USE_EXTEND please", lambda ev: None)
    types = [e.get("type") for e in seen]
    assert "tool_request" in types and "tool_result" in types and "done" in types, types
    req_evt = next(e for e in seen if e["type"] == "tool_request")
    assert req_evt["call"]["function"]["name"] == "extend_harness", req_evt
    res_evt = next(e for e in seen if e["type"] == "tool_result")
    assert res_evt["result"].get("decision") == "allow", res_evt["result"]

    ts = tools()
    greet = next((t for t in ts if t["name"] == "greet"), None)
    assert greet and greet["enabled"], f"greet not registered: {ts}"
    assert greet["kind"] == "command" and greet["params"] == ["who"]

    # ledger: global + per-chat (entries come newest-first; [0] is this run's)
    assert decisions("?kind=harness_extended")[0]["payload"].get("name") == "greet"
    d_chat = decisions(f"?chat_id={sid}&kind=harness_extended")
    assert d_chat and d_chat[0]["payload"].get("name") == "greet"
    print("1. extend_harness registered greet + ledgers OK")

    # ---------- 2. custom turn: model calls greet ----------
    seen = sse_chat(sid, "USE_CUSTOM please", lambda ev: None)
    types = [e.get("type") for e in seen]
    assert "tool_request" in types and "done" in types, types
    req_evt = next(e for e in seen if e["type"] == "tool_request")
    assert req_evt["call"]["function"]["name"] == "greet", req_evt
    args = json.loads(req_evt["call"]["function"]["arguments"])
    assert args == {"who": "world"}, args
    res_evt = next(e for e in seen if e["type"] == "tool_result")
    assert res_evt["result"].get("ok") is True, res_evt["result"]
    assert "hello world" in res_evt["result"].get("stdout", ""), res_evt["result"]

    d_chat = decisions(f"?chat_id={sid}&kind=custom_tool_call")
    assert d_chat and d_chat[0]["payload"].get("name") == "greet"
    print("2. custom tool greet ran (hello world) + ledger OK")

    # ---------- 3. user API CRUD ----------
    created = req("POST", "/api/tools", {
        "name": "touch_note", "kind": "command",
        "template": "touch notes/{name}", "params": ["name"],
        "description": "create a note file",
    })
    assert created["id"] and created["name"] == "touch_note"
    assert decisions("?kind=tool_added")[0]["payload"].get("name") == "touch_note"

    off = req("PUT", f"/api/tools/{created['id']}", {"enabled": False})
    assert off["enabled"] is False
    assert decisions("?kind=tool_updated")[0]["payload"].get("id") == created["id"]

    req("DELETE", f"/api/tools/{created['id']}")
    assert all(t["name"] != "touch_note" for t in tools())
    assert decisions("?kind=tool_removed")[0]["payload"].get("name") == "touch_note"
    print("3. /api/tools add/toggle/delete OK")

    # ---------- 4. negative: extend_harness outside extend mode ----------
    sid_w = make_session("extend-negative", "write")
    seen = sse_chat(sid_w, "USE_EXTEND please", lambda ev: None)
    res_evt = next(e for e in seen if e["type"] == "tool_result")
    err = res_evt["result"].get("error", "")
    assert "extend mode" in err, res_evt["result"]
    print("4. extend_harness denied in write mode OK")

    # ---------- 5. negative: disabled custom tool ----------
    greet_id = next(t for t in tools() if t["name"] == "greet")["id"]
    req("PUT", f"/api/tools/{greet_id}", {"enabled": False})
    seen = sse_chat(sid_w, "USE_CUSTOM please", lambda ev: None)
    res_evt = next(e for e in seen if e["type"] == "tool_result")
    assert res_evt["result"].get("decision") == "deny", res_evt["result"]
    req("PUT", f"/api/tools/{greet_id}", {"enabled": True})  # restore
    print("5. disabled custom tool denied OK")

    # ---------- 6. readonly sandbox semantics ----------
    sid_r = make_session("readonly-ext", "readonly")
    seen = sse_chat(sid_r, "READONLY_DENY please", lambda ev: None)
    res_evt = next(e for e in seen if e["type"] == "tool_result")
    assert res_evt["result"].get("decision") == "deny", res_evt["result"]

    seen = sse_chat(sid_r, "USE_CUSTOM please", lambda ev: None)
    res_evt = next(e for e in seen if e["type"] == "tool_result")
    # `echo` is a transparent read: sandbox allows it even in readonly
    assert res_evt["result"].get("ok") is True, res_evt["result"]
    print("6. readonly: write command denied, read tool allowed OK")

    print("EXTENSIONS E2E OK")


if __name__ == "__main__":
    main()
