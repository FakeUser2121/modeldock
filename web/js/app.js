/* modeldock front-end. Plain JS, no build step.
   Grown step by step: tabs/decisions/settings first, then servers, chat, agent. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || (opts.body != null ? "POST" : "GET"),
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    body: opts.body != null ? JSON.stringify(opts.body) : undefined,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    const msg = data && data.detail ? data.detail : data && data.message ? data.message : `HTTP ${res.status} ${path}`;
    throw new Error(msg);
  }
  return data;
}

let CONFIG = null;
async function loadConfig() {
  if (!CONFIG) CONFIG = await api("/api/config");
  return CONFIG;
}

/* ================= tabs ================= */
function bindTabs() {
  $$("#tabs .tab").forEach((b) =>
    b.addEventListener("click", () => {
      $$("#tabs .tab").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      $$(".tab-panel").forEach((p) => p.classList.remove("active"));
      $(`#tab-${b.dataset.tab}`).classList.add("active");
      const t = b.dataset.tab;
      if (t === "decisions") { refreshDecisionFilters(); loadDecisions(); }
      if (t === "settings") fillSettings();
      if (t === "servers") renderServers();
      if (t === "chat") refreshSessions();
    })
  );
}

async function health() {
  try {
    await api("/api/health");
    $("#header-status").textContent = "● connected";
  } catch {
    $("#header-status").textContent = "● offline";
  }
}

/* ================= decision log ================= */
async function loadDecisions() {
  const scope = $("#dec-scope").value || null;
  const kind = $("#dec-kind").value || null;
  const q = new URLSearchParams({ limit: 300, offset: 0 });
  if (scope) q.set("chat_id", scope);
  if (kind) q.set("kind", kind);
  try {
    const d = await api("/api/decisions?" + q);
    const tbody = $("#dec-table tbody");
    tbody.innerHTML = "";
    d.entries.forEach((e) => {
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td>${new Date(e.ts * 1000).toLocaleString()}</td>` +
        `<td>${esc(e.chat_id || "—")}</td>` +
        `<td>${esc(e.kind)}</td>` +
        `<td class="mono small"><details><summary>view</summary>` +
        `<pre>${esc(JSON.stringify(e.payload, null, 1))}</pre></details></td>`;
      tbody.appendChild(tr);
    });
  } catch (e) { console.warn("decisions:", e); }
}

async function refreshDecisionFilters() {
  try {
    const kinds = await api("/api/decisions/kinds");
    const sel = $("#dec-kind");
    const cur = sel.value;
    sel.innerHTML = `<option value="">All kinds</option>` +
      kinds.kinds.map((k) => `<option value="${esc(k)}">${esc(k)}</option>`).join("");
    sel.value = cur;
  } catch { /* endpoint not up yet */ }
  // scope options: global + every known session, so a chat's local ledger
  // can be viewed on its own
  try {
    const d = await api("/api/sessions");
    const sel = $("#dec-scope");
    const cur = sel.value;
    sel.innerHTML = `<option value="">Global (all chats)</option>` +
      (d.sessions || []).map((s) =>
        `<option value="${esc(s.id)}">${esc(s.title)} (${esc(s.id)})</option>`).join("");
    sel.value = cur;
  } catch { /* sessions not up yet */ }
}

/* ================= shared helpers ================= */
async function loadServerList() {
  try { return (await api("/api/servers")).servers || []; } catch { return []; }
}

function modelOptions(models) {
  return `<option value="">— pick model —</option>` +
    models.map((m) =>
      `<option value="${esc(m.id)}" data-vision="${m.vision ? "1" : "0"}">${esc(m.id)}${m.vision ? " (vision)" : ""}</option>`
    ).join("");
}

async function fetchModelsFor(serverId) {
  const d = await api(`/api/servers/${esc(serverId)}/models`);
  return d.models || [];
}

/* ================= settings ================= */
async function fillSettings() {
  await loadConfig();
  const d = CONFIG.defaults;
  const servers = await loadServerList();
  const srvSel = $("#cfg-default-server");
  srvSel.innerHTML = `<option value="">— none —</option>` +
    servers.map((x) => `<option value="${esc(x.id)}">${esc(x.name)}</option>`).join("");
  srvSel.value = d.default_server || "";
  const modelSel = $("#cfg-default-model");
  modelSel.innerHTML = `<option value="">— none —</option>`;
  if (d.default_server) {
    try {
      modelSel.innerHTML = modelOptions(await fetchModelsFor(d.default_server));
    } catch { /* discovery failed */ }
  }
  modelSel.value = d.default_model || "";
  $("#cfg-temperature").value = d.temperature;
  $("#cfg-top_p").value = d.top_p;
  $("#cfg-max_tokens").value = d.max_tokens;
  $("#cfg-repeat_penalty").value = d.repeat_penalty ?? "";
  $("#cfg-mode").value = d.mode;
  const agentSel = $("#cfg-agent");
  agentSel.innerHTML = (CONFIG.agent_types || [])
    .map((a) => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join("");
  agentSel.value = d.agent_type;
  $("#cfg-allowed").value = (CONFIG.allowed_outside_commands || []).join("\n");
  renderAgentTypes();
  renderMcp();
  renderTools();
}

function renderAgentTypes() {
  const wrap = $("#agent-types-list");
  wrap.innerHTML = (CONFIG.agent_types || []).map((a) => `
    <div class="card-row">
      <div>
        <b>${esc(a.name)}</b> <span class="muted small">(${esc(a.id)})</span>
        <button class="btn tiny" data-del-at="${esc(a.id)}">delete</button>
      </div>
      <textarea class="input at-prompt" data-id="${esc(a.id)}" rows="2">${esc(a.prompt)}</textarea>
    </div>`).join("");
  $$("#agent-types-list [data-del-at]").forEach((b) =>
    b.addEventListener("click", () => {
      CONFIG.agent_types = CONFIG.agent_types.filter((a) => a.id !== b.dataset.delAt);
      renderAgentTypes();
    })
  );
}

function renderMcp() {
  const wrap = $("#mcp-list");
  const servers = CONFIG.mcp_servers || [];
  wrap.innerHTML = servers.length
    ? servers.map((s) => `
      <div class="card-row">
        <div>
          <b>${esc(s.name)}</b>
          <span class="muted small">${esc(s.transport || "stdio")}</span>
          <label><input type="checkbox" data-mcp-ena="${esc(s.id)}" ${s.enabled ? "checked" : ""}> enabled</label>
          <button class="btn tiny" data-mcp-tools="${esc(s.id)}">tools</button>
          <button class="btn tiny" data-del-mcp="${esc(s.id)}">remove</button>
        </div>
        <div class="muted mono small" data-mcp-desc="${esc(s.id)}">${esc(s.command || s.url || "")} ${esc((s.args || []).join(" "))}</div>
      </div>`).join("")
    : `<div class="muted small">No MCP servers configured.</div>`;
  $$("#mcp-list [data-mcp-ena]").forEach((c) =>
    c.addEventListener("change", () => {
      const s = CONFIG.mcp_servers.find((x) => x.id === c.dataset.mcpEna);
      if (s) s.enabled = c.checked;
    })
  );
  $$("#mcp-list [data-del-mcp]").forEach((b) =>
    b.addEventListener("click", () => {
      CONFIG.mcp_servers = CONFIG.mcp_servers.filter((s) => s.id !== b.dataset.delMcp);
      renderMcp();
    })
  );
  $$("#mcp-list [data-mcp-tools]").forEach((b) =>
    b.addEventListener("click", async () => {
      const desc = $(`#mcp-list [data-mcp-desc="${b.dataset.mcpTools}"]`);
      if (!desc) return;
      b.disabled = true;
      try {
        const r = await api(`/api/mcp/servers/${b.dataset.mcpTools}/tools?refresh=true`);
        desc.textContent = (r.tools || []).map((t) => t.name).join(", ") || "no tools";
      } catch (e) {
        desc.textContent = `tools unavailable: ${e.message}`;
      }
      b.disabled = false;
    })
  );
}

$("#btn-settings-save").addEventListener("click", async () => {
  const d = CONFIG.defaults;
  d.temperature = parseFloat($("#cfg-temperature").value || 0.7);
  d.top_p = parseFloat($("#cfg-top_p").value || 1);
  d.max_tokens = parseInt($("#cfg-max_tokens").value || 2048, 10);
  d.repeat_penalty = $("#cfg-repeat_penalty").value === "" ? null : parseFloat($("#cfg-repeat_penalty").value);
  d.mode = $("#cfg-mode").value;
  d.agent_type = $("#cfg-agent").value;
  d.default_server = $("#cfg-default-server").value || null;
  d.default_model = $("#cfg-default-model").value || null;
  CONFIG.allowed_outside_commands = $("#cfg-allowed").value.split("\n").map((s) => s.trim()).filter(Boolean);
  (CONFIG.agent_types || []).forEach((a) => {
    const ta = $(`#agent-types-list .at-prompt[data-id="${a.id}"]`);
    if (ta) a.prompt = ta.value;
  });
  await api("/api/config", { body: CONFIG });
  $("#header-status").textContent = "settings saved";
});

$("#btn-at-add").addEventListener("click", () => {
  const id = $("#at-id").value.trim();
  const name = $("#at-name").value.trim();
  if (!id || !name) return;
  CONFIG.agent_types.push({ id, name, prompt: "You are a helpful assistant." });
  $("#at-id").value = "";
  $("#at-name").value = "";
  renderAgentTypes();
});

$("#btn-mcp-add").addEventListener("click", () => {
  const name = $("#mcp-name").value.trim();
  if (!name) return;
  const transport = $("#mcp-transport").value;
  const cmd = $("#mcp-cmd").value.trim();
  const url = $("#mcp-url").value.trim();
  const parts = cmd ? cmd.split(/\s+/) : [];
  CONFIG.mcp_servers.push({
    id: name.toLowerCase().replace(/[^a-z0-9]+/g, "_"),
    name,
    transport,
    command: parts[0] || "",
    args: parts.slice(1),
    url: url,
    env: {},
    enabled: true,
  });
  $("#mcp-name").value = ""; $("#mcp-cmd").value = ""; $("#mcp-url").value = "";
  renderMcp();
});

/* ================= custom tools (harness extensions) ================= */
async function renderTools() {
  const wrap = $("#tool-list");
  let tools = [];
  try {
    tools = (await api("/api/tools")).tools || [];
  } catch (e) {
    wrap.innerHTML = `<div class="muted small">tools unavailable: ${esc(e.message)}</div>`;
    return;
  }
  wrap.innerHTML = tools.length
    ? tools.map((t) => `
      <div class="card-row">
        <div>
          <b>${esc(t.name)}</b>
          <span class="badge">${esc(t.kind)}</span>
          <label><input type="checkbox" data-tool-ena="${esc(t.id)}" ${t.enabled ? "checked" : ""}> enabled</label>
          <button class="btn tiny" data-del-tool="${esc(t.id)}">remove</button>
        </div>
        <div class="muted mono small">
          ${esc(t.template || t.url || "")}
          <span class="muted">params: ${esc((t.params || []).join(", "))}</span>
          ${t.description ? `<div class="muted">${esc(t.description)}</div>` : ""}
          <div class="muted">source: ${esc(t.source || "user")}</div>
        </div>
      </div>`).join("")
    : `<div class="muted small">No custom tools yet — add one below, or ask the model in <b>extend</b> mode to register one.</div>`;
  $$("#tool-list [data-tool-ena]").forEach((c) =>
    c.addEventListener("change", async () => {
      try {
        await api(`/api/tools/${c.dataset.toolEna}`, { method: "PUT", body: { enabled: c.checked } });
      } catch (e) { /* ignore */ }
    })
  );
  $$("#tool-list [data-del-tool]").forEach((b) =>
    b.addEventListener("click", async () => {
      try {
        await api(`/api/tools/${b.dataset.delTool}`, { method: "DELETE" });
        renderTools();
      } catch (e) { /* ignore */ }
    })
  );
}

$("#btn-ct-add").addEventListener("click", async () => {
  const name = $("#ct-name").value.trim();
  const kind = $("#ct-kind").value;
  const template = $("#ct-template").value.trim();
  const params = $("#ct-params").value.split(",").map((s) => s.trim()).filter(Boolean);
  const desc = $("#ct-desc").value.trim();
  if (!name || !template || !params.length) {
    $("#header-status").textContent = "tool: need name, template and params";
    return;
  }
  try {
    await api("/api/tools", {
      body: { name, kind, template, url: kind === "http" ? template : "", params, description: desc },
    });
    $("#ct-name").value = ""; $("#ct-template").value = ""; $("#ct-params").value = ""; $("#ct-desc").value = "";
    renderTools();
  } catch (e) {
    $("#header-status").textContent = `tool: ${e.message}`;
  }
});

/* ================= sessions (chat) ================= */
let CURRENT_SESSION = null;
let ATTACHED_IMAGES = []; // data URLs attached to the next send

async function refreshSessions() {
  const ul = $("#session-list");
  let sessions = [];
  try {
    sessions = (await api("/api/sessions")).sessions || [];
  } catch (e) {
    ul.innerHTML = `<li class="muted">${esc(e.message)}</li>`;
    return;
  }
  refreshDecisionFilters(); // keep the decision scope dropdown in sync
  ul.innerHTML = "";
  if (!sessions.length) {
    ul.innerHTML = `<li class="muted">No sessions yet — create one above.</li>`;
    return;
  }
  sessions.forEach((s) => {
    const li = document.createElement("li");
    li.className = "session-item";
    li.dataset.id = s.id;
    li.innerHTML = `
      <div class="session-item-title">
        <b>${esc(s.title)}</b>
        <button class="btn tiny danger" data-del-session="${esc(s.id)}" title="Delete session">✕</button>
      </div>
      <div class="muted small mono">${esc(s.workspace)}</div>
      ${s.usage && s.usage.turns
        ? `<div class="muted small">${s.usage.turns} turns · ${s.usage.total_tokens} tokens</div>`
        : ""}`;
    ul.appendChild(li);
  });
  $$("#session-list .session-item").forEach((li) =>
    li.addEventListener("click", (ev) => {
      if (ev.target.closest("[data-del-session]")) return;
      openSession(li.dataset.id, li);
    })
  );
  $$("#session-list [data-del-session]").forEach((b) =>
    b.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      if (!confirm("Delete this session and its history?")) return;
      try {
        await api(`/api/sessions/${b.dataset.delSession}`, { method: "DELETE" });
        if (CURRENT_SESSION && CURRENT_SESSION.id === b.dataset.delSession) {
          CURRENT_SESSION = null;
          $("#chat-toolbar").classList.add("hidden");
        }
        refreshSessions();
      } catch (e) { alert(e.message); }
    })
  );
}

async function openSession(id, li) {
  // Resolve the sidebar item for this session; if the list predates the
  // session (probe race: page loaded before it was provisioned), refresh
  // once so the item exists and can be highlighted.
  let item = li;
  if (!item) item = $(`#session-list .session-item[data-id="${CSS.escape(id)}"]`);
  if (!item) {
    await refreshSessions();
    item = $(`#session-list .session-item[data-id="${CSS.escape(id)}"]`);
  }
  $$("#session-list .session-item").forEach((x) => x.classList.remove("active"));
  if (item) item.classList.add("active");
  api(`/api/sessions/${id}`)
    .then(async (s) => {
      CURRENT_SESSION = s;
      $("#composer-status").textContent = "";
      await renderHistory();
      fillChatToolbar();
      refreshContext();
      renderPendingApprovals();
    })
    .catch((e) => console.warn("open session:", e));
}

function addMessageEl(role, content, modelName) {
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  const meta = document.createElement("div");
  meta.className = "meta";
  meta.innerHTML = `<span>${role === "user" ? "you" : esc(modelName || "model")}</span>`;
  const body = document.createElement("div");
  body.className = "msg-body";
  body.textContent = content || "";
  div.appendChild(meta);
  div.appendChild(body);
  $("#messages").appendChild(div);
  return body;
}

async function renderHistory() {
  const wrap = $("#messages");
  wrap.innerHTML = "";
  if (!CURRENT_SESSION) return;
  let history = [];
  try {
    const d = await api(`/api/sessions/${CURRENT_SESSION.id}/history`);
    history = d.messages || [];
  } catch {
    history = [];
  }
  history.forEach((m) => {
    if (m.role === "user") {
      const body = addMessageEl("user", m.content || "");
      (m.images || []).forEach((im) => {
        const img = document.createElement("img");
        img.className = "msg-img";
        img.src = im;
        img.alt = "attached image";
        body.appendChild(img);
      });
    } else if (m.role === "assistant") addMessageEl("assistant", m.content, m.model);
    else if (m.role === "system" && m.compacted_from) {
      const div = document.createElement("div");
      div.className = "compact-note";
      div.textContent = `— context compacted: ${m.compacted_from} earlier messages summarized —`;
      $("#messages").appendChild(div);
    }
    // tool messages are rendered in the sandbox step
  });
  wrap.scrollTop = wrap.scrollHeight;
}

async function fillChatToolbar() {
  const s = CURRENT_SESSION;
  if (!s) return;
  $("#chat-toolbar").classList.remove("hidden");
  $("#chat-title").textContent = s.title;
  $("#chat-workspace").textContent = s.workspace;
  const st = s.settings || {};

  const serverSel = $("#sel-server");
  const servers = await loadServerList();
  serverSel.innerHTML = `<option value="">— pick server —</option>` +
    servers.map((x) => `<option value="${esc(x.id)}">${esc(x.name)}</option>`).join("");
  serverSel.value = st.server || "";
  if (serverSel.value === "") serverSel.selectedIndex = 0;

  const modelSel = $("#sel-model");
  modelSel.innerHTML = `<option value="">— pick model —</option>`;
  if (st.server) {
    try {
      modelSel.innerHTML = modelOptions(await fetchModelsFor(st.server));
      modelSel.value = st.model || "";
    } catch { /* discovery failed */ }
  }
  if (modelSel.value === "") modelSel.selectedIndex = 0;

  const modeSel = $("#sel-mode");
  modeSel.value = st.mode || "write";
  const agentSel = $("#sel-agent");
  await loadConfig();
  agentSel.innerHTML = (CONFIG.agent_types || []).map((a) =>
    `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join("");
  agentSel.value = st.agent_type || "general";

  $("#p-temperature").value = st.temperature ?? CONFIG.defaults.temperature;
  $("#p-top_p").value = st.top_p ?? CONFIG.defaults.top_p;
  $("#p-max_tokens").value = st.max_tokens ?? CONFIG.defaults.max_tokens;
  $("#p-repeat_penalty").value = st.repeat_penalty ?? "";
  $("#p-system_prompt").value = st.system_prompt ?? "";

  updateAttachVisibility();
}

function updateAttachVisibility() {
  const opt = $("#sel-model").selectedOptions[0];
  const vision = !!opt && opt.dataset.vision === "1";
  $("#btn-attach").classList.toggle("hidden", !vision);
  if (!vision) {
    ATTACHED_IMAGES = [];
    $("#composer-file").value = "";
    $("#composer-img").textContent = "";
  }
}

/* Composer selects: bound ONCE at boot; handlers read CURRENT_SESSION at
   event time, so a session switch never re-binds and leaks listeners. */
function bindComposerSelects() {
  const serverSel = $("#sel-server");
  const modelSel = $("#sel-model");
  const patchNow = () => {
    if (!CURRENT_SESSION) return;
    const patch = {
      server: serverSel.value || null,
      model: modelSel.value || null,
      mode: $("#sel-mode").value,
      agent_type: $("#sel-agent").value,
    };
    api(`/api/sessions/${CURRENT_SESSION.id}`, { method: "PUT", body: patch })
      .catch((e) => console.warn("save settings:", e));
  };
  serverSel.addEventListener("change", async () => {
    modelSel.innerHTML = `<option value="">— pick model —</option>`;
    if (serverSel.value) {
      try {
        modelSel.innerHTML = modelOptions(await fetchModelsFor(serverSel.value));
        // keep the chat's model if it still exists on the new server
        const cur = CURRENT_SESSION && (CURRENT_SESSION.settings || {}).model;
        if (cur && Array.from(modelSel.options).some((o) => o.value === cur)) {
          modelSel.value = cur;
        } else modelSel.selectedIndex = 0;
      } catch { modelSel.selectedIndex = 0; }
    } else modelSel.selectedIndex = 0;
    updateAttachVisibility();
    patchNow();
  });
  modelSel.addEventListener("change", () => {
    updateAttachVisibility();
    patchNow();
  });
  $("#sel-mode").addEventListener("change", patchNow);
  $("#sel-agent").addEventListener("change", patchNow);
}

/* ---------- compaction & fork ---------- */

async function refreshContext() {
  const meter = $("#ctx-meter");
  if (!meter || !CURRENT_SESSION) return;
  try {
    const [d, s] = await Promise.all([
      api(`/api/sessions/${CURRENT_SESSION.id}/context`),
      api(`/api/sessions/${CURRENT_SESSION.id}`),
    ]);
    CURRENT_SESSION = s; // keep cached meta (incl. usage) fresh
    let txt;
    if (d.context) {
      const pct = d.ratio == null ? "?" : Math.round(d.ratio * 100);
      const hot = d.would_auto_compact ? " · will auto-compact" : "";
      txt = `ctx ~${d.estimate}/${d.context} (${pct}%)${hot}`;
      meter.classList.toggle("hot", d.would_auto_compact);
    } else {
      txt = `ctx ~${d.estimate} (no context size set)`;
      meter.classList.remove("hot");
    }
    const u = s.usage;
    if (u && u.turns) txt += ` · ${u.turns} turns · ${u.total_tokens} tok`;
    meter.textContent = txt;
  } catch (e) {
    meter.textContent = "";
  }
}

$("#btn-compact").addEventListener("click", async () => {
  if (!CURRENT_SESSION) return;
  const btn = $("#btn-compact");
  btn.disabled = true;
  $("#composer-status").textContent = "compacting…";
  try {
    const d = await api(`/api/sessions/${CURRENT_SESSION.id}/compact`, { method: "POST", body: {} });
    $("#composer-status").textContent = d.compacted
      ? `compacted: ${d.summarized} messages summarized, ${d.kept} kept`
      : "nothing to compact yet";
    await renderHistory();
    refreshContext();
  } catch (e) {
    $("#composer-status").textContent = `compact failed: ${e.message}`;
  }
  btn.disabled = false;
});

$("#btn-fork").addEventListener("click", async () => {
  if (!CURRENT_SESSION) return;
  const btn = $("#btn-fork");
  btn.disabled = true;
  try {
    const s = await api(`/api/sessions/${CURRENT_SESSION.id}/fork`, { method: "POST", body: {} });
    $("#composer-status").textContent = `forked to ${s.id}`;
    await refreshSessions();
    const li = $(`#session-list .session-item[data-id="${s.id}"]`);
    if (li) openSession(s.id, li);
  } catch (e) {
    $("#composer-status").textContent = `fork failed: ${e.message}`;
  }
  btn.disabled = false;
});

$("#btn-new-session").addEventListener("click", () =>
  $("#new-session-form").classList.toggle("hidden")
);

$("#ns-cancel").addEventListener("click", () => $("#new-session-form").classList.add("hidden"));

$("#ns-create").addEventListener("click", async () => {
  const body = {
    title: $("#ns-title").value.trim(),
    workspace: $("#ns-workspace").value.trim(),
  };
  if (!body.workspace) return alert("Workspace path is required");
  try {
    await api("/api/sessions", { body });
    $("#new-session-form").classList.add("hidden");
    $("#ns-title").value = "";
    $("#ns-workspace").value = "";
    refreshSessions();
  } catch (e) { alert(e.message); }
});

function setComposerBusy(b) {
  $("#btn-send").disabled = b;
  $("#btn-send").style.opacity = b ? "0.5" : "";
}

function makeToolCard(call) {
  const fn = (call && call.function) || {};
  let args = {};
  try { args = JSON.parse(fn.arguments || "{}"); } catch { /* keep empty */ }
  const card = document.createElement("div");
  card.className = "tool-card";
  card.dataset.callId = (call && call.id) || "";
  let detail;
  if (fn.name === "run_command") {
    detail = (args.cmd || "") + (args.reason ? ` — ${args.reason}` : "");
  } else {
    detail = JSON.stringify(args, null, 1);
  }
  card.innerHTML =
    `<div class="t-head"><b>${esc(fn.name || "tool")}</b>` +
    `<span class="t-status pending">waiting…</span></div>` +
    `<pre>${esc(detail || "{}")}</pre>`;
  return card;
}

function updateToolCard(card, result) {
  if (!card) return;
  const st = $(".t-status", card);
  let txt, cls;
  if (result.ok) { txt = "ok"; cls = "ok"; }
  else if (result.decision === "deny") { txt = "denied (sandbox)"; cls = "err"; }
  else if (result.decision === "rejected") { txt = "rejected by you"; cls = "err"; }
  else { txt = "error"; cls = "err"; }
  if (st) { st.textContent = txt; st.className = `t-status ${cls}`; }
  const pre = $("pre", card);
  const detail = result.ok
    ? (result.stdout ?? result.output ?? "").trim() + (result.stderr ? `\n${result.stderr}` : "")
    : result.error || JSON.stringify(result);
  if (pre) pre.textContent = detail || "(no output)";
}

function makeApprovalBanner(req, approvalId) {
  const banner = document.createElement("div");
  banner.className = "approval-banner";
  const paths = (req.paths_outside || []).map(esc).join(", ");
  banner.innerHTML =
    `<div class="a-head">approval needed — this action leaves the chat's workspace</div>` +
    `<div class="a-body">command: ${esc(req.cmd || "")}\nreason: ${esc(req.reason || "")}\npaths outside: ${esc(paths || "—")}</div>` +
    `<textarea rows="2" placeholder="Message for the decision (optional)…"></textarea>` +
    `<div class="a-actions">` +
    `<button class="btn approve">approve</button>` +
    `<button class="btn danger reject">reject</button>` +
    `<span class="a-msg muted small"></span>` +
    `</div>`;
  const act = (approved) => async () => {
    const msg = $("textarea", banner).value.trim();
    const span = $(".a-msg", banner);
    $$(".a-actions .btn", banner).forEach((b) => (b.disabled = true));
    try {
      await api(`/api/sessions/${CURRENT_SESSION.id}/approve`, {
        body: { approval_id: approvalId, approved, message: msg },
      });
      banner.remove();
      $("#composer-status").textContent = approved ? "approved — continuing" : "rejected — continuing";
    } catch (e) {
      span.textContent = e.message;
      $$(".a-actions .btn", banner).forEach((b) => (b.disabled = false));
    }
  };
  $(".approve", banner).addEventListener("click", act(true));
  $(".reject", banner).addEventListener("click", act(false));
  return banner;
}

async function renderPendingApprovals() {
  if (!CURRENT_SESSION) return;
  let list = [];
  try {
    list = ((await api(`/api/sessions/${CURRENT_SESSION.id}/approvals`)).approvals) || [];
  } catch { return; }
  $$("#messages .approval-banner").forEach((b) => b.remove()); // idempotent re-render
  for (const a of list) {
    const banner = makeApprovalBanner(a.request, a.approval_id);
    $("#messages").appendChild(banner);
  }
}

async function sendMessage() {
  const input = $("#composer-input");
  const status = $("#composer-status");
  const text = input.value.trim();
  const images = ATTACHED_IMAGES;
  if (!text && images.length === 0) { status.textContent = "message is empty"; return; }
  if (!CURRENT_SESSION) { status.textContent = "open a session first"; return; }
  if ($("#btn-send").disabled) return;
  const serverId = $("#sel-server").value;
  const modelId = $("#sel-model").value;
  if (!serverId || !modelId) {
    status.textContent = "pick a server and model first";
    return;
  }
  input.value = "";
  input.style.height = "auto";
  const userBody = addMessageEl("user", text || "(image)");
  images.forEach((im) => {
    const img = document.createElement("img");
    img.className = "msg-img";
    img.src = im;
    img.alt = "attached image";
    userBody.appendChild(img);
  });
  const body = addMessageEl("assistant", "", modelId);
  status.textContent = "thinking…";
  setComposerBusy(true);
  try {
    const res = await fetch(`/api/sessions/${CURRENT_SESSION.id}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, images }),
    });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      throw new Error(d.detail || `HTTP ${res.status}`);
    }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    let content = "";
    const cards = new Map(); // call_id -> tool card element
    const pump = () =>
      reader.read().then(({ value, done }) => {
        if (done) return;
        buf += dec.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const line = buf.slice(0, idx).replace(/^data: /, "");
          buf = buf.slice(idx + 2);
          let ev;
          try { ev = JSON.parse(line); } catch { continue; }
          if (ev.type === "delta") {
            content += ev.content;
            body.textContent = content;
            const wrap = $("#messages");
            wrap.scrollTop = wrap.scrollHeight;
          } else if (ev.type === "tool_request") {
            const card = makeToolCard(ev.call);
            body.insertAdjacentElement("beforebegin", card);
            cards.set(card.dataset.callId || `call_${cards.size}`, card);
            status.textContent = "tool requested — sandbox decides";
          } else if (ev.type === "tool_result") {
            updateToolCard(cards.get(ev.call_id), ev.result);
            status.textContent = ev.result.ok ? "tool ran ok" : `tool ${ev.result.decision || "failed"}`;
          } else if (ev.type === "approval_request") {
            const banner = makeApprovalBanner(ev.request, ev.approval_id);
            body.insertAdjacentElement("beforebegin", banner);
            status.textContent = "waiting for your approval…";
          } else if (ev.type === "compacted") {
            status.textContent = `auto-compacted: ${ev.before} -> ${ev.after} messages`;
            refreshContext();
          } else if (ev.type === "done") {
            status.textContent = ev.usage
              ? `done · ${ev.usage.total_tokens ?? "?"} tokens`
              : "done";
            refreshContext();
            refreshSessions(); // keep the sidebar's "N turns · M tokens" fresh
          } else if (ev.type === "error") {
            status.textContent = `error: ${ev.message}`;
            body.textContent = content || `(no reply — ${ev.message})`;
          }
        }
        return pump();
      });
    await pump();
    if (!content) body.textContent = "(empty reply)";
  } catch (e) {
    status.textContent = `error: ${e.message}`;
    body.textContent = body.textContent || `(request failed: ${e.message})`;
  } finally {
    setComposerBusy(false);
    ATTACHED_IMAGES = [];
    $("#composer-file").value = "";
    $("#composer-img").textContent = "";
  }
}

$("#btn-send").addEventListener("click", sendMessage);
// Plain Enter sends; Shift+Enter inserts a newline
$("#composer-input").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && !ev.shiftKey) {
    ev.preventDefault();
    sendMessage();
  }
});

$("#btn-attach").addEventListener("click", () => {
  if (!$("#btn-attach").classList.contains("hidden")) $("#composer-file").click();
});
$("#composer-file").addEventListener("change", async () => {
  const files = Array.from($("#composer-file").files || []);
  const imgs = [];
  for (const f of files) {
    if (!f.type.startsWith("image/")) continue;
    if (f.size > 8 * 1024 * 1024) continue; // keep payloads sane
    const dataUrl = await new Promise((res, rej) => {
      const r = new FileReader();
      r.onload = () => res(r.result);
      r.onerror = rej;
      r.readAsDataURL(f);
    });
    imgs.push(dataUrl);
  }
  ATTACHED_IMAGES = imgs;
  $("#composer-img").textContent = imgs.length ? `${imgs.length} image${imgs.length === 1 ? "" : "s"} attached` : "";
});

/* ================= servers ================= */
let SERVERS = [];

async function renderServers() {
  const wrap = $("#server-list");
  try {
    const d = await api("/api/servers");
    SERVERS = d.servers || [];
  } catch (e) {
    wrap.innerHTML = `<div class="muted">${esc(e.message)}</div>`;
    return;
  }
  if (!SERVERS.length) {
    wrap.innerHTML = `<div class="muted">No servers yet — add one above.</div>`;
    return;
  }
  wrap.innerHTML = "";
  for (const s of SERVERS) {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <div class="row spread">
        <div><b>${esc(s.name)}</b> <span class="muted mono small">${esc(s.url)}</span>
          <span id="sv-health-${esc(s.id)}" class="muted small">checking…</span></div>
        <div class="row">
          <label class="muted small">ctx <input class="input ctx-size" data-id="${esc(s.id)}"
            type="number" value="${s.context_size ?? ""}" placeholder="server default"></label>
          <button class="btn tiny" data-refresh-srv="${esc(s.id)}">Refresh models</button>
          <button class="btn tiny danger" data-del-srv="${esc(s.id)}">Remove</button>
        </div>
      </div>
      <div class="row muted small" id="sv-models-${esc(s.id)}">discovering models…</div>`;
    wrap.appendChild(card);
    checkServerHealth(s.id);
    loadServerModels(s.id);
  }
  $$("#server-list [data-refresh-srv]").forEach((b) =>
    b.addEventListener("click", () => loadServerModels(b.dataset.refreshSrv, true))
  );
  $$("#server-list [data-del-srv]").forEach((b) =>
    b.addEventListener("click", async () => {
      if (!confirm(`Remove server ${b.dataset.delSrv}?`)) return;
      try {
        await api(`/api/servers/${b.dataset.delSrv}`, { method: "DELETE" });
        renderServers();
      } catch (e) { alert(e.message); }
    })
  );
  $$("#server-list .ctx-size").forEach((inp) =>
    inp.addEventListener("change", async () => {
      const id = inp.dataset.id;
      const v = inp.value === "" ? null : parseInt(inp.value, 10);
      try {
        await api(`/api/servers/${id}`, { method: "PUT", body: { context_size: v } });
        const s = SERVERS.find((x) => x.id === id);
        if (s) s.context_size = v;
      } catch (e) { alert(e.message); }
    })
  );
}

async function checkServerHealth(id) {
  const el = $(`#sv-health-${esc(id)}`);
  if (!el) return;
  try {
    const h = await api(`/api/servers/${esc(id)}/health`);
    el.innerHTML = `<span class="dot ${h.ok ? "ok" : "err"}"></span>${h.ok ? "online" : "offline"}`;
  } catch (e) {
    el.innerHTML = `<span class="dot err"></span>unreachable`;
  }
}

async function loadServerModels(id, refresh = false) {
  const el = $(`#sv-models-${esc(id)}`);
  if (!el) return;
  try {
    const d = await api(`/api/servers/${esc(id)}/models?refresh=${refresh ? 1 : 0}`);
    const models = d.models || [];
    el.innerHTML =
      `<b>${models.length}</b> model${models.length === 1 ? "" : "s"}` +
      models
        .map(
          (m) => `<div class="row">
            <span class="mono small">${esc(m.id)}</span>
            ${m.vision ? `<span class="badge">vision</span>` : ""}
          </div>`
        )
        .join("") +
      (models.length ? "" : ` <i>(none found — is the server running?)</i>`);
  } catch (e) {
    el.innerHTML = `<span class="muted">model discovery failed: ${esc(e.message)}</span>`;
  }
}

$("#btn-add-server").addEventListener("click", () =>
  $("#server-form").classList.toggle("hidden")
);

$("#sv-cancel").addEventListener("click", () => $("#server-form").classList.add("hidden"));

$("#sv-save").addEventListener("click", async () => {
  const body = {
    name: $("#sv-name").value.trim(),
    url: $("#sv-url").value.trim(),
    api_key: $("#sv-key").value,
    context_size: $("#sv-ctx").value === "" ? null : parseInt($("#sv-ctx").value, 10),
    description: $("#sv-desc").value.trim(),
  };
  if (!body.url) return alert("Server URL is required");
  try {
    await api("/api/servers", { body });
    $("#server-form").classList.add("hidden");
    ["#sv-name", "#sv-url", "#sv-key", "#sv-ctx", "#sv-desc"].forEach((s) => ($(s).value = ""));
    renderServers();
  } catch (e) { alert(e.message); }
});

/* ================= boot ================= */
document.addEventListener("DOMContentLoaded", async () => {
  bindTabs();
  bindComposerSelects(); // bound once, for the whole session
  $("#btn-dec-refresh").addEventListener("click", loadDecisions);
  await health();
  refreshDecisionFilters();
  await refreshSessions();
  const first = $("#session-list .session-item");
  if (first) openSession(first.dataset.id, first); // auto-open the first chat
});
