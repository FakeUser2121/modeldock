#!/usr/bin/env python3
"""Generate web/_probe.html: the real UI markup (index.html) with app.js
replaced by a synchronous stress+measurement script.

Run headless:  firefox --headless --dump-dom http://127.0.0.1:8787/_probe.html
The probe appends <pre id="probe-out">PROBE {json}</pre> to the body.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = (ROOT / "web" / "index.html").read_text()

PROBE_SCRIPT = """<script>
(function () {
  const T = (n) => "x".repeat(n);
  const $ = (s, r) => (r || document).querySelector(s);
  const res = { page: null, els: [] };
  const finish = (txt) => {
    const out = document.createElement("pre");
    out.id = "probe-out";
    out.textContent = "PROBE " + txt;
    document.body.appendChild(out);
    // cross-origin no-cors POST: body still reaches the result server
    fetch("http://127.0.0.1:8891/result", { method: "POST", mode: "no-cors", body: txt });
  };
  try {
    // 1) activate hidden containers so they are laid out
    $("#tab-decisions").classList.add("active");
    $("#chat-toolbar").classList.remove("hidden");
    // 2) stress content: long unbreakable tokens everywhere text can go
    const msgs = $("#messages");
    const mk = (cls, body) => {
      const d = document.createElement("div");
      d.className = "msg " + cls;
      d.innerHTML = '<div class="meta"><span>m</span></div><div class="msg-body"></div>';
      d.querySelector(".msg-body").textContent = body;
      msgs.appendChild(d);
    };
    mk("assistant", "prefix " + T(400) + " suffix with spaces");
    mk("user", "short " + T(180));
    const card = document.createElement("div");
    card.className = "tool-card";
    card.innerHTML = '<div class="t-head"><b>run_command</b><span class="t-status pending">waiting</span></div><pre></pre>';
    card.querySelector("pre").textContent = "cd " + T(250) + " && echo hi\\n" + T(300);
    msgs.appendChild(card);
    const banner = document.createElement("div");
    banner.className = "approval-banner";
    banner.innerHTML = '<div class="a-head">h</div><div class="a-body"></div>';
    banner.querySelector(".a-body").textContent = "command: " + T(300) + "\\nreason: " + T(120);
    msgs.appendChild(banner);
    $("#chat-title").textContent = "title " + T(300);
    $("#chat-workspace").textContent = "/ws/" + T(300);
    const tr = document.createElement("tr");
    tr.innerHTML = '<td>2026-01-01 00:00:00</td><td>chat</td><td>tool_call</td><td class="mono small"><details open><summary>view</summary><pre></pre></details></td>';
    tr.querySelector("pre").textContent = JSON.stringify({ cmd: "run " + T(400), reason: T(200) }, null, 1);
    $("#dec-table tbody").appendChild(tr);
    const li = document.createElement("li");
    li.innerHTML = '<div class="session-item-title"><b>sess ' + T(200) + '</b><button class="btn tiny">x</button></div><div class="s-sub">/ws/' + T(250) + "</div>";
    $("#session-list").appendChild(li);
    // 3) measure synchronously (layout is computed on demand)
    const de = document.documentElement;
    res.page = { scrollWidth: de.scrollWidth, clientWidth: de.clientWidth, overflow: de.scrollWidth - de.clientWidth };
    const sels = [".msg", ".msg-body", ".tool-card", ".tool-card pre", ".approval-banner",
      ".approval-banner .a-body", "#dec-table", ".table pre", "#dec-table-wrap", ".panel-pad",
      ".chat-sidebar", ".chat-main", ".chat-title", ".chat-workspace", ".chat-head",
      ".toolbar-row", "#messages", "#composer", "#session-list", ".session-item-title",
      ".s-sub", "header", "main"];
    for (const s of sels)
      for (const el of document.querySelectorAll(s)) {
        const sw = el.scrollWidth, cw = el.clientWidth;
        if (sw > cw + 1)
          res.els.push({ sel: s, sw: sw, cw: cw, ovx: getComputedStyle(el).overflowX });
      }
    finish(JSON.stringify(res));
  } catch (e) {
    finish("ERROR " + e.message);
  }
})();
</script>
"""

out = INDEX.replace('<script src="/js/app.js"></script>', PROBE_SCRIPT)
(ROOT / "web" / "_probe.html").write_text(out)
print("probe written:", len(out), "bytes")
