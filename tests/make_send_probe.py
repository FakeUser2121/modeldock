#!/usr/bin/env python3
"""Generate web/_sendprobe.html: the real UI + real app.js, plus a probe
script that types "hello" into the composer, dispatches a real Enter keydown,
and verifies the assistant reply renders in the DOM.

The page is served by the backend itself (http://127.0.0.1:8787/_sendprobe.html)
so all /api calls are same-origin. The probe POSTs its result JSON to
127.0.0.1:8891 (no-cors), which writes .probe-result.json.
"""
import json
import os
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = (ROOT / "web" / "index.html").read_text()
BASE = os.environ.get("MODELDOCK_API", "http://127.0.0.1:8787")

# provision a session with server+model already set, so the Enter path
# has nothing to complain about
req = urllib.request.Request(
    BASE + "/api/sessions",
    data=json.dumps(
        {
            "title": "send-probe",
            "workspace": str(ROOT / ".test-ws-send"),
            "server": os.environ.get("PROBE_SERVER", "dfc1c605"),
            "model": os.environ.get("PROBE_MODEL", "qwen3.8-27b-iq3-64k-dcfr"),
        }
    ).encode(),
    headers={"Content-Type": "application/json"},
)
SID = json.load(urllib.request.urlopen(req, timeout=30))["id"]
print("provisioned session:", SID)

PROBE_SCRIPT = """<script>
(function () {
  const $ = (s) => document.querySelector(s);
  const SID = """ + json.dumps(SID) + """;
  const out = (txt) => {
    const p = document.createElement("pre");
    p.id = "probe-out";
    p.textContent = "PROBE " + txt;
    document.body.appendChild(p);
    fetch("http://127.0.0.1:8891/result", { method: "POST", mode: "no-cors", body: txt });
  };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  (async () => {
    try {
      // wait for the app's own boot (it auto-opens the first session)
      let guard = 0;
      while (!CURRENT_SESSION && guard++ < 100) await sleep(200);
      if (!CURRENT_SESSION) return out("NO_SESSION");
      // open the provisioned session unless boot already opened it
      if (CURRENT_SESSION.id !== SID) {
        const li = document.querySelector('#session-list .session-item[data-id="' + SID + '"]');
        if (!li) return out("NO_LI_FOR_SESSION");
        openSession(SID, li);
      }
      // wait until the session fetch has RESOLVED and the toolbar is filled —
      // until then openSession/renderHistory can still wipe #messages
      guard = 0;
      while (
        (CURRENT_SESSION.id !== SID || !$("#sel-server").value || !$("#sel-model").value) &&
        guard++ < 100
      ) await sleep(200);
      if (CURRENT_SESSION.id !== SID) return out("OPEN_FAILED");
      await sleep(1500); // let any pending open/toolbar work settle
      // the real user path: type, then press Enter
      const input = $("#composer-input");
      input.value = "hello";
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
      // wait until the turn ends (status done/error) and read the final DOM
      let reply = "";
      let status = "";
      let userText = "";
      guard = 0;
      while (guard++ < 1500) { // up to 10 min: reasoning models take minutes per turn
        await sleep(400);
        status = $("#composer-status").textContent;
        const users = document.querySelectorAll("#messages .msg.user .msg-body");
        userText = users.length ? users[users.length - 1].textContent : "";
        const as = document.querySelectorAll("#messages .msg.assistant .msg-body");
        reply = as.length ? as[as.length - 1].textContent : "";
        if (/done|error/.test(status)) break;
      }
      out(JSON.stringify({
        sent: userText === "hello" && reply !== "" && /done/.test(status) && !/error/.test(status),
        user_rendered: userText,
        reply: reply.slice(0, 300),
        status: status,
        composer_cleared: input.value === ""
      }));
    } catch (e) {
      out("ERROR " + e.message);
    }
  })();
})();
</script>
"""

out = INDEX.replace("</body>", PROBE_SCRIPT + "\n</body>")
(ROOT / "web" / "_sendprobe.html").write_text(out)
print("probe written:", len(out), "bytes")
