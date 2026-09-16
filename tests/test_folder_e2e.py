"""End-to-end test for the chat folder system.

Every chat lives in the folder it was started in:
  <workspace>/.modeldock/<chat_id>/  (session.json, messages.json, decisions.jsonl)
data/registry.json keeps chats enumerable across arbitrary folders.
Legacy chats in data/sessions/<chat_id> must keep working without a registry entry.

Requires modeldock on 8787.
"""
import json
import shutil
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8787"
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
WS = ROOT / ".test-ws-folder"
WS2 = ROOT / ".test-ws-folder2"


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(r) as resp:
        return json.loads(resp.read().decode())


def main():
    # clean slates
    for p in (WS, WS2):
        shutil.rmtree(p, ignore_errors=True)
    (DATA / "registry.json").unlink(missing_ok=True)

    # --- 1. create in a chosen folder ---------------------------------
    s = req("POST", "/api/sessions", {"title": "folder-test", "workspace": str(WS)})
    sid = s["id"]
    print("session:", sid, "->", s["workspace"])
    assert s["workspace"] == str(WS)

    d = WS / ".modeldock" / sid
    assert d.is_dir(), f"chat folder missing: {d}"
    assert (d / "session.json").is_file(), "session.json not in chat folder"
    assert (d / "decisions.jsonl").is_file(), "decisions.jsonl not in chat folder"
    decs = [json.loads(l) for l in (d / "decisions.jsonl").read_text().splitlines() if l]
    assert any(x["kind"] == "session_created" for x in decs), "session_created not in chat ledger"
    assert all(x["chat_id"] == sid for x in decs)

    reg = json.loads((DATA / "registry.json").read_text())
    assert sid in reg and reg[sid]["workspace"] == str(WS), f"registry entry wrong: {reg}"
    print("records live in the chosen folder + registry entry: ok")

    # legacy sessions must still be listed (no registry entry)
    legacy_ids = {p.name for p in (DATA / "sessions").iterdir() if p.is_dir()}
    sessions = req("GET", "/api/sessions")["sessions"]
    listed = {x["id"] for x in sessions}
    missing = {i for i in legacy_ids if i not in listed}
    assert not missing, f"legacy sessions not listed: {missing}"
    print(f"legacy sessions still listed: {sorted(legacy_ids)}")

    # --- 2. per-chat decisions endpoint reads from the new location ----
    p = req("GET", f"/api/decisions?chat_id={sid}")["entries"]
    assert p and all(x["chat_id"] == sid for x in p)
    g = [x for x in req("GET", "/api/decisions")["entries"] if x["chat_id"] == sid]
    assert g, "global ledger missing this chat"
    print("per-chat + global ledgers consistent:", len(p), len(g))

    # --- 3. fork inherits the same folder -----------------------------
    fork = req("POST", f"/api/sessions/{sid}/fork", {})
    fid = fork["id"]
    assert fork["workspace"] == str(WS), "fork must stay in the same folder"
    fd = WS / ".modeldock" / fid
    assert (fd / "session.json").is_file(), "fork records not in the same folder"
    reg = json.loads((DATA / "registry.json").read_text())
    assert reg[fid]["workspace"] == str(WS)
    fh = req("GET", f"/api/sessions/{fid}/history")["messages"]
    sh = req("GET", f"/api/sessions/{sid}/history")["messages"]
    assert len(fh) == len(sh)
    print("fork lives in the same folder: ok")

    # --- 4. workspace change moves the records ------------------------
    WS2.mkdir(parents=True, exist_ok=True)
    upd = req("PUT", f"/api/sessions/{sid}", {"workspace": str(WS2)})
    assert upd["workspace"] == str(WS2)
    assert not d.exists(), "old chat folder still present after move"
    d2 = WS2 / ".modeldock" / sid
    assert (d2 / "session.json").is_file() and (d2 / "decisions.jsonl").is_file()
    reg = json.loads((DATA / "registry.json").read_text())
    assert reg[sid]["workspace"] == str(WS2)
    moved = [json.loads(l) for l in (d2 / "decisions.jsonl").read_text().splitlines() if l]
    assert any(x["kind"] == "session_updated" for x in moved), "session_updated not in moved ledger"
    p2 = req("GET", f"/api/decisions?chat_id={sid}")["entries"]
    assert p2 and all(x["chat_id"] == sid for x in p2)
    print("workspace move relocated records + registry: ok")

    # --- 5. delete cleans up folder and registry ----------------------
    req("DELETE", f"/api/sessions/{sid}")
    assert not d2.exists(), "chat folder left behind after delete"
    req("DELETE", f"/api/sessions/{fid}")
    assert not fd.exists(), "fork folder left behind after delete"
    reg = json.loads((DATA / "registry.json").read_text())
    assert sid not in reg and fid not in reg, f"registry not cleaned: {reg}"
    ids = {x["id"] for x in req("GET", "/api/sessions")["sessions"]}
    assert sid not in ids and fid not in ids
    print("delete removed folders + registry entries: ok")

    # --- 6. legacy chat still openable after all of this --------------
    if legacy_ids:
        lid = sorted(legacy_ids)[0]
        meta = req("GET", f"/api/sessions/{lid}")
        hist = req("GET", f"/api/sessions/{lid}/history")["messages"]
        assert meta["id"] == lid and isinstance(hist, list)
        print(f"legacy chat {lid} still openable, {len(hist)} messages")

    # clean slate again
    for p in (WS, WS2):
        shutil.rmtree(p, ignore_errors=True)

    print("\nALL FOLDER-SYSTEM E2E CHECKS PASSED")


if __name__ == "__main__":
    main()
