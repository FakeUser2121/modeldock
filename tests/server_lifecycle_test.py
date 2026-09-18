"""Server-layer lifecycle test: real FastAPI app, in-process browser, lifespan teardown.

Run:  python tests/server_lifecycle_test.py
Uses the scratch workspace under modeldock/data/smoke-ws.
Verifies: health -> session -> browser routes with a LIVE in-process sidecar,
frame JPEG over HTTP, and lifespan teardown on app shutdown (no orphan chromium).
"""
import sys, time, json, os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from fastapi.testclient import TestClient
from harness.main import app
from harness import browser as br

ws = os.path.join(ROOT, "data", "smoke-ws")
url = "data:text/html," + (
    "<html><body style='margin:0;background:white'>"
    "<div id='b' style='position:absolute;left:5px;top:40px;width:120px;height:40px;background:teal'></div>"
    "<script>var x=5;var b=document.getElementById('b');"
    "setInterval(function(){x=x+5>1000?0:x+5;b.style.left=x+'px';},50);"
    "</script></body></html>"
)

with TestClient(app) as c:
    r = c.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"]
    meta = c.post("/api/sessions", json={"title": "srv-lifecycle", "workspace": ws}).json()
    cid = meta["id"]
    print("CID", cid, flush=True)

    r = c.get(f"/api/sessions/{cid}/browser")
    print("STATUS0", json.dumps(r.json()), flush=True)
    assert r.json()["running"] is False

    r = c.get(f"/api/sessions/{cid}/browser/frame")
    print("FRAME0", r.status_code, flush=True)
    assert r.status_code == 404

    t0 = time.time()
    st = br.ensure_started(cid, url)  # same module the HTTP routes wrap; registered in THIS process
    print("START t=%.1f" % (time.time() - t0), json.dumps(st), flush=True)

    r = c.get(f"/api/sessions/{cid}/browser")
    print("STATUS1", json.dumps(r.json()), flush=True)
    assert r.json()["running"] is True and r.json()["pid"]

    br.send(cid, "screencast_start")
    time.sleep(6.0)
    r = c.get(f"/api/sessions/{cid}/browser")
    print("MID", json.dumps(r.json()), flush=True)

    r = c.get(f"/api/sessions/{cid}/browser/frame")
    print("FRAME1", r.status_code, r.headers.get("content-type"), len(r.content), r.content[:4].hex(), flush=True)
    assert r.status_code == 200 and r.content[:2] == b"\xff\xd8"

    r = c.get(f"/api/sessions/{cid}/browser")
    print("STATUS2", json.dumps(r.json()), flush=True)

# context exit -> lifespan teardown -> shutdown_all
time.sleep(2.0)
r = c.get(f"/api/sessions/{cid}/browser")
print("AFTER", json.dumps(r.json()), flush=True)
print("DONE cid=" + cid)
