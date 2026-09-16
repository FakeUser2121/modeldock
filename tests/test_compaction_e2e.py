"""End-to-end test for step 7: compaction (auto + manual) and fork.

Requires the mock OpenAI server on 8901 and modeldock on 8787.
"""
import json
import urllib.request

BASE = "http://127.0.0.1:8787"
SERVER_ID = "743f1927"
MODEL = "mock-model-a"
WS = "/home/user3/Documents/unsafe/modeldock/.test-ws"


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(r) as resp:
        return json.loads(resp.read().decode())


def chat_raw(sid, message):
    data = json.dumps({"message": message}).encode()
    r = urllib.request.Request(
        BASE + f"/api/sessions/{sid}/chat", data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(r) as resp:
        return resp.read().decode()


def events(raw):
    out = []
    for chunk in raw.split("\n\n"):
        if chunk.startswith("data: "):
            out.append(json.loads(chunk[6:]))
    return out


def main():
    # fresh session
    s = req("POST", "/api/sessions", {"title": "compact-test", "workspace": WS})
    sid = s["id"]
    print("session:", sid)
    req("PUT", f"/api/sessions/{sid}",
        {"server": SERVER_ID, "model": MODEL, "mode": "write"})

    # small context size so the 85% threshold is reachable
    req("PUT", f"/api/servers/{SERVER_ID}", {"context_size": 600})

    ctx = req("GET", f"/api/sessions/{sid}/context")
    print("context after set:", ctx)
    assert ctx["context"] == 600 and ctx["threshold"] == 0.85

    filler = "Tell me about the weather in Paris today. " + "x" * 320
    compacted_event = None
    for i in range(1, 7):
        raw = chat_raw(sid, filler)
        evs = events(raw)
        c = next((e for e in evs if e.get("type") == "compacted"), None)
        if c:
            compacted_event = c
            print(f"turn {i}: COMPACTED event:", c)
            break
        ctx = req("GET", f"/api/sessions/{sid}/context")
        print(f"turn {i}: ctx est={ctx['estimate']} ratio={ctx['ratio']} "
              f"would={ctx['would_auto_compact']} msgs={ctx['messages']}")

    assert compacted_event, "auto-compaction never triggered"
    assert compacted_event["mode"] == "auto"
    assert compacted_event["after"] < compacted_event["before"]

    # history: summary system message first, then recent kept
    h = req("GET", f"/api/sessions/{sid}/history")["messages"]
    print("history after auto-compact:",
          [(m["role"], len(m.get("content") or ""), m.get("compacted_from")) for m in h])
    assert h[0]["role"] == "system" and h[0].get("compacted_from")
    assert h[0]["content"].startswith("[Compacted history]")
    assert h[1]["role"] == "user"  # kept window starts at a user message

    # turn still works after compaction
    raw = chat_raw(sid, filler)
    evs = events(raw)
    assert any(e.get("type") == "done" for e in evs), "turn after compaction failed"
    print("post-compact turn ok")

    # manual compaction
    before = req("GET", f"/api/sessions/{sid}/history")["messages"]
    res = req("POST", f"/api/sessions/{sid}/compact", {})
    print("manual compact:", {k: res[k] for k in ("compacted", "summarized", "kept")})
    assert res["compacted"] and res["summarized"] > 0
    after = req("GET", f"/api/sessions/{sid}/history")["messages"]
    assert len(after) < len(before)
    assert after[0]["role"] == "system" and after[0].get("compacted_from")
    print("manual compact ok")

    # ledgers
    g = [d for d in req("GET", "/api/decisions")["entries"] if d["kind"] == "compaction"]
    p = [d for d in req("GET", f"/api/decisions?chat_id={sid}")["entries"] if d["kind"] == "compaction"]
    print("global compactions:", len(g), "per-chat:", len(p))
    # the tiny context stays over 85% after the first auto-compact, so the
    # post-compact turn legitimately auto-compacts again; at least one auto
    # and exactly one manual are guaranteed
    pkinds = [d.get("payload", {}).get("trigger") for d in p]
    assert len(p) >= 2 and "auto" in pkinds and pkinds.count("manual") == 1
    kinds = {(d["kind"], d.get("payload", {}).get("trigger")) for d in g}
    assert ("compaction", "auto") in kinds and ("compaction", "manual") in kinds
    assert all(d.get("chat_id") == sid for d in p)

    # fork
    fork = req("POST", f"/api/sessions/{sid}/fork", {})
    fid = fork["id"]
    print("fork:", fid, fork["title"], fork["workspace"])
    assert fork["workspace"] == WS
    assert fork["settings"]["server"] == SERVER_ID
    assert fork["settings"]["model"] == MODEL
    fh = req("GET", f"/api/sessions/{fid}/history")["messages"]
    sh = req("GET", f"/api/sessions/{sid}/history")["messages"]
    assert len(fh) == len(sh), "fork history mismatch"
    print("fork history len:", len(fh))

    # fork is independent: compaction on the original does not touch the fork
    res2 = req("POST", f"/api/sessions/{sid}/compact", {})
    fh2 = req("GET", f"/api/sessions/{fid}/history")["messages"]
    assert len(fh2) == len(fh)
    print("fork unaffected by later compaction:", len(fh2))

    fk = [d for d in req("GET", f"/api/decisions?chat_id={fid}")["entries"] if d["kind"] == "session_forked"]
    gf = [d for d in req("GET", "/api/decisions")["entries"] if d["kind"] == "session_forked"]
    assert fk and fk[0]["payload"]["from"] == sid
    assert gf and any(d["chat_id"] == fid for d in gf)
    print("session_forked recorded in both ledgers")

    # negative: no context size -> no auto compaction, context endpoint says unset
    req("PUT", f"/api/servers/{SERVER_ID}", {"context_size": None})
    ctx = req("GET", f"/api/sessions/{sid}/context")
    print("context after unset:", ctx)
    assert ctx["context"] is None and ctx["ratio"] is None
    raw = chat_raw(sid, filler)
    evs = events(raw)
    assert not any(e.get("type") == "compacted" for e in evs), "compact fired without context size"
    assert any(e.get("type") == "done" for e in evs)
    print("no auto-compact without context size: ok")

    print("\nALL STEP-7 E2E CHECKS PASSED")


if __name__ == "__main__":
    main()
