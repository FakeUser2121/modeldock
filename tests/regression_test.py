"""Regression tests for the bugs fixed in the hardening pass.

Every test here failed before the corresponding fix. No LLM server, no
Chromium and no Go toolchain required: the CDP sidecar is replaced with a
tiny fake that speaks the same JSONL protocol.

Run:  python tests/regression_test.py        (or: pytest tests/regression_test.py)
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_DATA = tempfile.mkdtemp(prefix="modeldock-regress-")
os.environ["MODELDOCK_DATA"] = _DATA

FAKE_SIDECAR = r'''#!/usr/bin/env python3
"""Stand-in for mdock-cdp: JSONL in, JSONL out. "slow" replies after 3s."""
import sys, json, time, threading
def handle(c):
    if c.get("cmd") == "slow":
        time.sleep(3.0)
    sys.stdout.write(json.dumps({"id": c.get("id"), "ok": True,
                                 "value": {"cmd": c.get("cmd"), "url": "about:blank"}}) + "\n")
    sys.stdout.flush()
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    c = json.loads(line)
    threading.Thread(target=handle, args=(c,), daemon=True).start()
    if c.get("cmd") == "close":
        time.sleep(0.2)
        break
'''

_fake = Path(_DATA) / "fake-cdp"
_fake.write_text(FAKE_SIDECAR)
_fake.chmod(0o755)
os.environ["MODELDOCK_CDP_BIN"] = str(_fake)

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def workspace(name):
    p = Path(_DATA) / "ws" / name
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


# --------------------------------------------------------------- config

def test_config():
    print("config: missing sections are back-filled, cache is not shared")
    from harness import config as cfg

    p = cfg.DATA_DIR / "config.json"
    cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
    # a config.json written before `browser` / `agent_types` existed
    p.write_text(json.dumps({
        "servers": [{"id": "s1"}],
        "defaults": {"temperature": 0.2},
    }))
    cfg._set_cache(None, None)
    c = cfg.load()
    check("browser section restored", "browser" in c)
    check("agent_types restored (else every chat loses its persona prompt)",
          bool(c.get("agent_types")))
    check("user's own servers preserved", c["servers"] == [{"id": "s1"}])
    check("user's own defaults preserved", c["defaults"]["temperature"] == 0.2)
    check("absent defaults filled in", c["defaults"].get("mode") == "write")

    # a customised section must not be clobbered by the merge
    p.write_text(json.dumps({"agent_types": [{"id": "mine", "name": "Mine", "prompt": "x"}]}))
    cfg._set_cache(None, None)
    check("customised agent_types kept verbatim",
          [a["id"] for a in cfg.load()["agent_types"]] == ["mine"])

    a = cfg.load()
    a.setdefault("mcp_servers", []).append({"id": "leak"})
    check("mutating one caller's copy does not corrupt the cache",
          cfg.load().get("mcp_servers") == [])


# -------------------------------------------------------------- sandbox

def test_sandbox():
    print("sandbox: confinement holds, and the shell tool is a real shell")
    from harness import sandbox

    ws = workspace("sandbox")
    Path(ws, "src").mkdir(exist_ok=True)

    def act(cmd, mode="write"):
        return sandbox.decide(mode, cmd, ws, [])["action"]

    # escapes that used to be allowed outright
    check("relative traversal needs approval", act("cat ../../../etc/passwd") == "approval")
    check("bash -c payload is inspected",
          act("bash -c 'rm -rf /etc/hosts'") == "approval")
    check("python -c payload is inspected",
          act("python3 -c \"open('/etc/x','w')\"") == "approval")
    check("'rm -rf ..' needs approval", act("rm -rf ..") == "approval")
    check("second segment of a chain is judged",
          act("echo ok && rm -rf ~/stuff") == "approval")
    check("shell-expanded path needs approval", act("cat $HOME/.ssh/id_rsa") == "approval")
    check("command substitution is inspected",
          act("cat $(echo /etc/passwd)") == "approval")

    # ordinary in-workspace work must stay frictionless
    check("pipes allowed", act("ls | head") == "allow")
    check("redirect inside workspace allowed", act("echo hi > out.txt") == "allow")
    check("cd+chain inside workspace allowed", act("cd src && ls") == "allow")
    check("relative subdir allowed", act("grep -rn foo src/") == "allow")

    # readonly
    check("readonly denies writes", act("rm -rf .", "readonly") == "deny")
    check("readonly denies redirection", act("echo x > y", "readonly") == "deny")
    check("readonly allows reads", act("cat src/a.txt", "readonly") == "allow")
    check("readonly allows pipelines of reads", act("ls | wc -l", "readonly") == "allow")

    # real execution
    r = sandbox.run_command("echo hi > out.txt && cat out.txt", ws)
    check("redirection actually writes", r["ok"] and r["stdout"] == "hi",
          f"got {r}")
    check("pipes work", sandbox.run_command("printf 'a\\nb\\n' | grep b", ws)["stdout"] == "b")
    check("cd chains work", sandbox.run_command("cd src && pwd", ws)["stdout"].endswith("/src"))
    r = sandbox.run_command("sleep 30 | sleep 30", ws, timeout=2)
    check("timeout kills the whole pipeline", r.get("timed_out") is True and not r["ok"])


# ---------------------------------------------------------------- tools

def test_tools():
    print("tools: optional paths resolve, search_files works")
    from harness import tools

    ws = workspace("tools")
    Path(ws, "a.py").write_text("def alpha():\n    return 1\n")
    Path(ws, "b.txt").write_text("alpha beta\n")
    Path(ws, "bin").mkdir(exist_ok=True)
    Path(ws, "bin", "blob").write_bytes(b"\x00\x01\x02alpha")
    sess = {"id": "t1", "workspace": ws, "settings": {"mode": "write"}}

    r = tools._execute_builtin("list_files", {}, sess)
    check("bare list_files is not denied as 'path is empty'", r.get("ok") is True, str(r))
    check("list_files '.' resolves",
          tools._execute_builtin("list_files", {"path": "."}, sess).get("ok") is True)

    r = tools._execute_builtin("search_files", {"pattern": "alpha"}, sess)
    check("search_files finds matches", r["ok"] and r["count"] >= 2, str(r)[:120])
    check("search_files reports file:line",
          all("file" in m and "line" in m for m in r["matches"]))
    check("search_files skips binaries",
          not any(m["file"].endswith("blob") for m in r["matches"]))
    r = tools._execute_builtin("search_files", {"pattern": "alpha", "glob": "*.py"}, sess)
    check("search_files honours glob", {m["file"] for m in r["matches"]} == {"a.py"})
    r = tools._execute_builtin("search_files", {"pattern": "[unclosed"}, sess)
    check("bad regex is an error, not a crash", r["ok"] is False and "regular expression" in r["error"])
    r = tools._execute_builtin("search_files", {"pattern": "nothinghere"}, sess)
    check("empty result carries a recovery hint", bool(r.get("note")))

    ro = {"id": "t1", "workspace": ws, "settings": {"mode": "readonly"}}
    check("search_files allowed in readonly",
          tools._execute_builtin("search_files", {"pattern": "alpha"}, ro).get("ok") is True)
    check("write_file denied in readonly",
          tools._execute_builtin("write_file", {"path": "x", "content": "y"}, ro)["decision"] == "deny")
    check("tool round budget raised", tools.MAX_TOOL_ROUNDS >= 24)


# ------------------------------------------------------------ supervisor

def test_supervisor():
    print("supervisor: responses are correlated, lifecycle is clean")
    import harness.browser.supervisor as sup
    from harness import browser as br
    from harness import chat as ch

    cid = ch.create_session("regress", workspace("browser"))["id"]
    br.ensure_started(cid, "about:blank")
    b = sup._registry[cid]

    # one timed-out command used to desync the protocol permanently
    try:
        b._send({"cmd": "slow"}, timeout=1.0)
        timed_out = False
    except sup.BrowserError:
        timed_out = True
    check("slow command times out", timed_out)
    time.sleep(3.5)  # the abandoned reply lands here and must be discarded
    check("next command gets its own reply",
          (b.send("status").get("value") or {}).get("cmd") == "status")
    check("and the one after that too",
          (b.send("eval").get("value") or {}).get("cmd") == "eval")

    # a long command must not wedge the status route the pane polls
    threading.Thread(target=lambda: b.send("slow"), daemon=True).start()
    time.sleep(0.3)
    t0 = time.time()
    st = br.status(cid)
    elapsed = time.time() - t0
    check("status stays responsive during a long command", elapsed < 1.5, f"{elapsed:.2f}s")
    check("status says it is degraded", st.get("busy") is True)
    time.sleep(3.2)

    # a dead sidecar must fail fast rather than block for the full timeout
    b.proc.kill()
    time.sleep(0.4)
    t0 = time.time()
    try:
        b.send("status")
        died_fast = False
    except sup.BrowserError:
        died_fast = time.time() - t0 < 5
    check("dead sidecar fails fast", died_fast)
    check("status reports it stopped", br.status(cid)["running"] is False)

    br.close(cid)
    check("closed browser is evicted from the registry", cid not in sup._registry)

    # deleting a chat must not strand a browser
    cid2 = ch.create_session("regress2", workspace("browser2"))["id"]
    br.ensure_started(cid2, "about:blank")
    ch.delete_session(cid2)
    check("deleting a session tears its browser down", cid2 not in sup._registry)

    check("no fake sidecars left running",
          subprocess.run(["pgrep", "-f", "fake-cdp"], capture_output=True).returncode != 0)


# ---------------------------------------------------------------- routes

def test_routes():
    from fastapi.testclient import TestClient

    from harness.main import app

    print("routes: browser endpoints, asset guard, fork index")
    c = TestClient(app)
    sid = c.post("/api/sessions", json={"title": "r", "workspace": workspace("routes")}).json()["id"]

    check("browser status route", c.get(f"/api/sessions/{sid}/browser").json()["running"] is False)
    check("frame route 404s before a frame exists",
          c.get(f"/api/sessions/{sid}/browser/frame").status_code == 404)
    check("browser close route", c.post(f"/api/sessions/{sid}/browser/close").json()["closed"] is True)

    check("css asset served", c.get("/css/app.css").status_code == 200)
    check("unknown asset 404s", c.get("/css/nope.css").status_code == 404)
    check("dotfile asset refused", c.get("/css/.env").status_code == 404)

    check("empty body is a 400, not a 500 traceback",
          c.post(f"/api/sessions/{sid}/chat").status_code == 400)
    check("malformed body is a 400",
          c.post(f"/api/sessions/{sid}/chat", content=b"{oops").status_code == 400)
    check("fork without a body works", c.post(f"/api/sessions/{sid}/fork").status_code == 200)
    check("non-integer fork index is a 400",
          c.post(f"/api/sessions/{sid}/fork", json={"up_to_index": "x"}).status_code == 400)


# ----------------------------------------------------------------- chat

def test_chat():
    print("chat: history survives a missing folder")
    from harness import chat as ch

    ws = workspace("chat")
    cid = ch.create_session("hist", ws)["id"]
    import shutil
    shutil.rmtree(ch.session_dir(cid), ignore_errors=True)
    ch.save_history(cid, [{"role": "user", "content": "hi"}])
    check("history is written even if the folder vanished",
          ch.get_history(cid) == [{"role": "user", "content": "hi"}])


def main():
    for fn in (test_config, test_sandbox, test_tools, test_supervisor, test_routes, test_chat):
        fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all regression checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
