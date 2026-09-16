"""End-to-end test for the approval gate.

Requires the mock OpenAI server on 8901 and modeldock on 8787.
The mock's "USE_OUTSIDE" trigger asks to run `cat /etc/passwd` (outside the
workspace), so the turn must suspend at an approval_request, wait for the
user's verdict, then run the tool and finish.
"""
import json
import threading
import urllib.request

BASE = "http://127.0.0.1:8787"
WS = "/home/user3/Documents/unsafe/modeldock/.test-ws"


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(r) as resp:
        return json.loads(resp.read().decode())


def sse_stream(path, body, on_event):
    data = json.dumps(body).encode()
    r = urllib.request.Request(BASE + path, data=data, method="POST",
        headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(r)
    try:
        buf = b""
        while True:
            chunk = resp.read(1)
            if not chunk:
                return
            buf += chunk
            if buf.endswith(b"\n\n"):
                line = buf.decode().strip()
                if line.startswith("data: "):
                    payload = line[6:]
                    if payload == "[DONE]":
                        return
                    on_event(json.loads(payload))
                buf = b""
    finally:
        resp.close()


def main():
    # find the mock server by its URL
    servers = req("GET", "/api/servers").get("servers", [])
    mock = next((s for s in servers if s.get("url", "").startswith("http://127.0.0.1:8901")), None)
    assert mock, "mock server (127.0.0.1:8901) not configured"

    s = req("POST", "/api/sessions", {"title": "approval-smoke", "workspace": WS})
    sid = s["id"]
    req("PUT", f"/api/sessions/{sid}",
        {"server": mock["id"], "model": "mock-model-a", "mode": "write"})

    approval_pending = threading.Event()
    approval_id = {}
    seen = []

    def on_event(ev):
        seen.append(ev.get("type"))
        print("SSE:", ev.get("type"), str(ev)[:110])
        if ev["type"] == "approval_request":
            approval_id["id"] = ev["approval_id"]
            approval_pending.set()

    t = threading.Thread(target=sse_stream,
        args=(f"/api/sessions/{sid}/chat", {"message": "USE_OUTSIDE please"}, on_event))
    t.start()
    approval_pending.wait(10)
    assert approval_id.get("id"), "no approval_request seen"

    res = req("POST", f"/api/sessions/{sid}/approve",
              {"approval_id": approval_id["id"], "approved": True,
               "message": "go ahead, read it"})
    assert res.get("approved") is True
    t.join()
    print("event sequence:", seen)

    # the user's note must land in the ledger
    d = req("GET", f"/api/decisions?chat_id={sid}&kind=approval_decided")
    entries = d["entries"]
    assert entries and entries[0]["payload"].get("message") == "go ahead, read it"
    assert entries[0]["payload"].get("approved") is True
    print("approval_decided ledger:", entries[0]["payload"])

    assert "done" in seen, "turn did not finish after approval"
    print("APPROVAL E2E OK")


if __name__ == "__main__":
    main()
