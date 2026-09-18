"""E2E smoke for the per-chat browser supervisor (stage 2).

Run:  python tests/browser_smoke.py
Uses a scratch workspace under modeldock/data/smoke-ws; no server needed.
Verifies: start -> screencast -> frame -> eval -> close -> status, no orphan chromium.
"""
import sys, time, os, json
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from harness import browser as br
from harness import chat as ch

ws = os.path.join(ROOT, "data", "smoke-ws")
os.makedirs(ws, exist_ok=True)
meta = ch.create_session("stage2-smoke", ws)
cid = meta["id"]
print("CHAT", cid, flush=True)
print("STATUS0", json.dumps(br.status(cid)), flush=True)

url = "data:text/html," + (
    "<html><body style='margin:0;background:white'>"
    "<div id='b' style='position:absolute;left:5px;top:40px;width:120px;height:40px;background:teal'></div>"
    "<script>var x=5;var b=document.getElementById('b');"
    "setInterval(function(){x=x+5>1000?0:x+5;b.style.left=x+'px';},50);"
    "</script></body></html>"
)
t0 = time.time()
st = br.ensure_started(cid, url)
print("START t=%.1f" % (time.time() - t0), json.dumps(st), flush=True)
r = br.send(cid, "screencast_start")
print("CAST", json.dumps(r), flush=True)
time.sleep(4.0)
st2 = br.status(cid)
print("MID", json.dumps(st2), flush=True)
data = br.frame_bytes(cid)
print("FRAME", (len(data), data[:4].hex()) if data else None, flush=True)
r = br.send(cid, "eval", js="document.getElementById('b').style.left")
print("EVAL", json.dumps(r), flush=True)
br.send(cid, "screencast_stop")
c = br.close(cid)
print("CLOSE", json.dumps(c), flush=True)
print("END", json.dumps(br.status(cid)), flush=True)
