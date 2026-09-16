# modeldock

A web-GUI harness for running local LLM servers. Point it at your local model
servers (llama.cpp `llama-server`, vLLM, LM Studio, TabbyAPI — anything
OpenAI-compatible), it auto-discovers every model behind each URL, and lets
you chat with any of them from one UI.

## Run (with uv)

```bash
cd modeldock
uv run python run.py
```

`uv` creates a project venv and installs dependencies on first run.
The GUI then lives at **http://127.0.0.1:8787**.

### Standalone window (no browser)

The same UI also runs in its own native window, with no browser process:

```bash
uv run python gui.py
```

It boots the FastAPI app in-process on a loopback port, then opens a
pywebview (WebKit2GTK) window that hosts the exact same `web/` files.
Needs a display (`DISPLAY`); the window is resizable (min 720x480) and
exits cleanly on close. Set `MODELDOCK_PORT` to change its port.

## Quick start

1. Open http://127.0.0.1:8787 → **Servers** tab → *Add server*:
   paste the URL of any OpenAI-compatible endpoint
   (e.g. `http://127.0.0.1:8080` for `llama-server`,
   `http://127.0.0.1:8000/v1` for vLLM) plus an API key if the server wants one.
   Models are auto-detected from `GET /v1/models`; health + model lists
   refresh on demand.
2. **Chat** tab → *New session*: pick a title and a workspace folder on your
   machine. New sessions inherit the **global defaults** (server, model,
   mode, agent type, params, system prompt) from Settings — set them once,
   every new chat starts with them, and nothing you pick in a chat is ever
   asked again.
3. Chat. Pick the server and model in the composer row under the input
   (per-chat — it stays that model until you change it there). Plain **Enter**
   sends, **Shift+Enter** inserts a newline. If the model supports images
   (auto-detected, see below), the attach button appears — pick images and
   they are sent with the message and shown in the chat history.
   The context meter in the toolbar shows estimated context usage,
   the auto-compaction warning, and the session's running token usage.

## Features

- **Servers tab** — add server URLs + optional API keys; models auto-detected
  via `GET /v1/models`; per-server context size (the server's `context_size`).
  Every model is probed once for **vision support**: the harness sends a
  small test image and marks image-capable models, so the model list shows a
  ` (vision)` badge and the composer's attach button only appears for them.
  Caps are probed once, cached per server, and ledgered.
- **Chat** — each session is bound to a workspace folder on your machine
  (`data/sessions/<id>/` holds the meta, history, and its own ledger).
  Per-chat local settings (server, model, params, mode, agent type, system
  prompt) persist — the chat keeps its chosen server+model until you change
  them there; global defaults live in Settings and are inherited by every
  new chat (an explicit server/model at creation time overrides them).
  Images sent by the user are stored with the history entry and rendered in
  the chat.
- **Agent modes** — `readonly` / `write` / `autonomous` / `extend`.
  - The model can only act inside the chat's workspace.
  - Anything touching outside requires an explicit approval request
    (what, where, why) that you approve or reject with a message —
    pending approvals are visible from any tab and the turn waits for you.
  - `autonomous` additionally allows an exact-string whitelist of
    outside commands (`allowed_outside_commands` in Settings) that run
    without asking.
  - `extend` behaves like `write` but additionally lets the model
    extend the harness itself: it can add, update, or remove custom
    tools (shell-command or HTTP tools) via the `extend_harness` tool.
    Every extension event is written to both decision ledgers; custom
    tools also appear in Settings → Custom tools, where you can edit
    or delete them yourself (they are just entries in `data/extensions.json`).
- **Decision ledgers** — every decision (turns, tool calls, approvals,
  server changes, MCP calls, compactions, forks, session changes) is
  recorded separately in a global ledger (`data/decisions-global.jsonl`)
  and, for chat-bound events, a per-chat ledger
  (`data/sessions/<chat>/decisions.jsonl`). The Decisions tab browses both
  (filter by chat and by kind).
- **MCP** — add/remove/enable/disable MCP servers over stdio or streamable
  HTTP; their tools are exposed as `mcp__<server>__<tool>` and enabling a
  server is your explicit consent to its tools. A dead MCP server never
  breaks a turn.
- **Compaction** — manual button, or automatic at 85% of the model's
  configured context size (the chat's own model summarizes the older
  messages; the recent window is kept verbatim). Only auto-triggers when a
  context size is actually set.
- **Fork** — fork any chat into a new session: same workspace, same
  settings, full history copied, independent from that point on.
- **Agent types** — editable persona prompts (id, name, prompt) in Settings;
  each chat picks one in its toolbar.
- **Custom tools** — command- or HTTP-backed tools the model can call in any
  non-readonly mode; created by you in Settings or by the model itself in
  `extend` mode; fully editable/removable through the UI (`/api/tools`).

## Layout

```
modeldock/
├── pyproject.toml          # uv project
├── run.py                  # browser entry point
├── gui.py                  # standalone native window (pywebview)
├── harness/
│   ├── main.py             # FastAPI app + routes
│   ├── config.py           # global config store (persisted, mtime-cached)
│   ├── decisions.py        # global + per-chat decision ledgers
│   ├── servers.py          # server registry + model discovery (TTL-cached)
│   ├── adapters/           # per-protocol adapters (openai-compat, ...)
│   ├── chat.py             # per-chat sessions (workspaces, history,
│   │                       #   local settings, usage totals, fork)
│   ├── turn.py             # the agent turn: tools, approvals,
│   │                       #   auto-compaction, history, ledgers
│   ├── tools.py            # agent tools + native/JSON-fallback tool calls
│   ├── extensions.py       # custom tools store (data/extensions.json)
│   ├── sandbox.py          # dir confinement + mode policies
│   ├── approvals.py        # approve/reject gate with notes
│   ├── mcp.py              # MCP client registry (stdio + http)
│   └── compaction.py       # context compaction (manual + auto)
├── web/                    # no-build web GUI (HTML/CSS/JS)
└── data/                   # config.json, decision ledgers, sessions/
```

### Resource usage

The harness is intentionally lightweight: no polling loops, no background
timers — everything is driven by requests.

- Global config and MCP tool lists are cached by file mtime / TTL, so
  repeated reads are cheap.
- Decision ledgers stream with a bounded sliding window: listing N newest
  entries uses O(N) memory regardless of ledger size.
- OpenAI adapters and MCP clients are cached per server id and dropped
  when a server is removed; compaction uses a one-shot adapter that is
  always closed.
- Approvals pending on a disconnected client are cleaned up on disconnect,
  so nothing outlives its turn.

## Tests

`tests/` holds mock servers (OpenAI-compatible, MCP stdio/HTTP) and
end-to-end scripts. They need the mocks running:

```bash
# terminal 1
uv run python tests/mock_server.py        # port 8901
# terminal 2
uv run python tests/mock_mcp.py           # port 8902
# terminal 3
uv run python run.py                      # port 8787
# terminal 4
uv run python tests/test_compaction_e2e.py
uv run python tests/test_approval_e2e.py
uv run python tests/test_extensions_e2e.py
```

UI overflow is checked numerically (no vision needed): `tests/make_probe.py`
generates `web/_probe.html` from the real UI markup with worst-case
unbreakable tokens injected; `tests/probe_view.py` opens it in a WebKit
window that POSTs `scrollWidth`/`clientWidth` measurements of every text
container to `tests/result_server.py`. All containers must report zero
overflow (long tokens wrap via `overflow-wrap: anywhere`, wide tables and
code blocks scroll inside their own boxes).

A functional send probe runs the real app in the same WebKit window:
`tests/make_send_probe.py` provisions a session with a real server+model,
writes `web/_sendprobe.html` (served by the backend at
`/_sendprobe.html`), and the probe types "hello", dispatches a real Enter
keydown, and reports whether the assistant reply renders in the DOM.
`tests/test_llama_e2e.py` is the same flow against a real llama.cpp server
(SSE text turn, per-chat persistence, image turn, history images, global
default inheritance).
