"""E2E against the real llama.cpp server (http://127.0.0.1:5800/v1).

Checks:
1. chat round-trip (text) with SSE events
2. per-chat model/server persistence across turns
3. image message (vision) round-trip
4. global-default inheritance into a brand-new chat
"""
import os
import sys

import httpx

BASE = "http://127.0.0.1:8787"
SERVER_ID = "dfc1c605"
MODEL = "qwen3.8-27b-iq3-64k-dcfr"
WS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".test-ws-llama")

PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAD1fKIgAAAADElEQVR42mNkYGBgAAEAAAqAApUAAQBXjSJJAAAAAElFTkSuQmCC"


def read_sse(res):
    events = []
    for line in res.iter_lines():
        line = line.strip()
        if not line.startswith("data: "):
            continue
        try:
            events.append(__import__("json").loads(line[6:]))
        except Exception:
            pass
    return events


def main():
    c = httpx.Client(base_url=BASE, timeout=900)  # reasoning models: minutes per turn

    # 1. text round-trip
    s = c.post("/api/sessions", json={
        "title": "llama-e2e", "workspace": WS,
        "server": SERVER_ID, "model": MODEL,
    }).json()
    sid = s["id"]
    print("session:", sid)
    res = c.post(f"/api/sessions/{sid}/chat", json={"message": "Reply with exactly: pong"})
    evs = read_sse(res)
    types = [e["type"] for e in evs]
    done = [e for e in evs if e["type"] == "done"]
    assert res.status_code == 200, res.status_code
    assert "start" in types and "done" in types, types
    assert done and done[0].get("assistant"), "no assistant text"
    print("text turn ok:", types)
    # stream must carry real token usage (stream_options include_usage)
    u = done[0].get("usage") or {}
    assert u.get("total_tokens", 0) > 0, "no usage in stream done event"
    print("stream usage ok:", u)

    # 2. per-chat persistence: settings must still carry server+model
    meta = c.get(f"/api/sessions/{sid}").json()
    assert meta["settings"]["server"] == SERVER_ID, meta["settings"]
    assert meta["settings"]["model"] == MODEL, meta["settings"]
    print("per-chat settings persist: server=%s model=%s" % (meta["settings"]["server"], meta["settings"]["model"]))

    # 3. image round-trip
    res = c.post(f"/api/sessions/{sid}/chat", json={
        "message": "Describe the image in one word.", "images": [PNG],
    })
    evs = read_sse(res)
    done = [e for e in evs if e["type"] == "done"]
    assert res.status_code == 200, res.status_code
    assert done and done[0].get("assistant"), "no assistant reply to image"
    print("image turn ok:", done[0]["assistant"][:80])

    # history keeps the image with the user turn
    hist = c.get(f"/api/sessions/{sid}/history").json()["messages"]
    img_entries = [m for m in hist if m.get("images")]
    assert img_entries, "no image entry in history"
    print("history image entries:", len(img_entries))

    # 4. global-default inheritance
    cfg = c.get("/api/config").json()
    cfg["defaults"]["default_server"] = SERVER_ID
    cfg["defaults"]["default_model"] = MODEL
    c.put("/api/config", json=cfg)
    s2 = c.post("/api/sessions", json={"title": "llama-e2e-defaults", "workspace": WS}).json()
    st = s2.get("settings", {})
    assert st.get("server") == SERVER_ID, st
    assert st.get("model") == MODEL, st
    print("new chat inherited defaults: server=%s model=%s" % (st.get("server"), st.get("model")))
    c.delete(f"/api/sessions/{s2['id']}")

    print("LLAMA E2E OK")


if __name__ == "__main__":
    main()
