# Integration Plan: Ego-Lite as a first-class ModelDock agent tool

Status: PLAN (repo under jj; no code changes yet beyond .gitignore + this doc).
Constraint set honored: live browser view INSIDE the ModelDock window beside the chat (no separate OS
window); browser forced to CPU/RAM only, never GPU; extremely low resource use when the browser is
unused; integrate with ModelDock's existing sandbox + approval system; keep as much Ego-Lite
functionality as practical; language split Rust (deterministic, low-memory) / Go (async, network) /
Python (glue); tmux + bacon for Rust compile checking.
Fallback rule (user): if the Ego-Lite-based integration does not work after integrating, roll back via
jj and re-plan around https://github.com/vercel-labs/agent-browser.

## 0. Research summary (source-tagged)

| Fact | Tag | Source |
|---|---|---|
| Ego-Lite = open MIT repo (Node.js CDP harness + agent skill); browser is a separate free macOS app (DMG) | Verified | https://github.com/citrolabs/ego-lite, https://lite.ego.app |
| App is macOS-only today; Windows/Linux on roadmap | Verified | repo README + https://lite.ego.app/roadmap |
| Agent interface: `ego-browser` CLI (installed by app, ~/.local/bin; skill-only via `npx skills add citrolabs/ego-lite`); invoked `ego-browser nodejs <<'EOF' ... EOF` (Node ESM); every invocation = new Node process; spaces/tabs persist, JS vars do not | Verified | skills/ego-browser/SKILL.md v2.0.0 |
| v2 API surface: profiles(), listTaskSpaces(), taskSpace(nameOrId,{profileId?}), claimTaskSpace(id), takeOverTaskSpace(id), task.page(label)/pages()/tabs()/newPage(), task.finish({keep}), task.cdp(method,params,{timeout}); page.goto(url,{waitUntil:commit|domcontentloaded|load(default)|networkidle})/reload/snapshot({scope:full_page|only_within_viewport(default),includeStableLocator?})/screenshot({path,fullPage,clip,scale,raw})/url/title/info/waitForURL/waitForEvent(popup|download)/waitForTimeout/waitForSelector/waitForLoadState/waitForFunction/evaluate/fetch/cdp(method,params,{timeout})/events()/acceptDialog/dismissDialog/click/type/hover/drag/scroll(+label)/fileChooser.setFiles | Verified | package/ego-browser/src/public-api-schema.ts (via skills/ego-browser/references/api.md) |
| Snapshot = compressed accessibility tree with `@N` SDK-assigned refs bound to frame/document/backend node; persist across rounds via page ref registry/ledger; stale ref => fresh snapshot; v1 `@e` refs gone. Compat selectors: css=, :has-text, :text-is, >> nth=N, loc=role:...[name*="..."] | Verified | api.md |
| CDP escape: page.cdp = Target-domain CDP; task.cdp = Browser/Target-domain CDP | Verified | api.md |
| Transport: `globalThis.ego` native binding inside closed-source app; `ego.sendCDPMessage`; session attach 2s TTL; buffered events 10k cap; JS dialog tracking | Verified | package/ego-browser/browser-runtime.ts, AGENTS.md, package README |
| Embedded host contract: call `disposeEgoSdk()` before discarding embedded Node context (rejects pending CDP, clears Page/session state, releases onCDPMessage ownership) | Verified | docs/native-sdk-lifecycle-requirement.md |
| Design: no daemon, no CDP port opened by the app; docs flag remote-debugging ports as exposing full local browser control; Chrome 136+ requires non-default user-data-dir for remote-debugging port (context) | Verified (design claim) / context (Chrome 136 rule) | lite.ego.app compare page; https://lite.ego.app/blog/hermes-with-wings-what-ego-lite-unlocks |
| Skills install to ~/.agents/skills/; custom agent harnesses just need to be able to run `ego-browser` on the host | Verified | https://lite.ego.app/document/en/docs/custom-agent-harness |
| Benchmark (vendor): 31 tasks, 93.5% perfect, $1.75/task, 30.3 turns vs agent-browser 45.6 | Verified (vendor benchmark) | https://lite.ego.app/compare/ego-lite-vs-agent-browser |
| This machine: Linux x86_64, Wayland, Chromium 153.0.8010.36 (user-installed), ChromeDriver 153, go 1.27.1, cargo 1.97.1, tmux 3.7b | Verified locally | shell |
| SwiftShader flags for CPU-only Chromium: `--use-gl=swiftshader --enable-unsafe-swiftshader` (software Vulkan/GL bundled with Chrome; opt-in flag for trusted envs; works with --headless=new on Linux) | Verified via community sources; must be re-verified on this box (Stage 1) | github.com/omacom/omarchy/discussions/2366, discussion.fedoraproject.org t/167520, zenn.dev/syoyo/articles/4 |

## 1. Architecture

```
ModelDock web UI (web/) — SAME window as chat
  [chat column] | [browser pane: live frame <img>, status, open/close]
      served by run.py (127.0.0.1:8787) or gui.py pywebview (same assets)
        |
FastAPI harness (harness/main.py)
  POST /api/sessions/{chat}/chat          (SSE turn loop)        [existing]
  POST /api/sessions/{chat}/approve       (approval gate)        [existing, reused]
  GET  /api/sessions/{chat}/browser/status                     [NEW]
  GET  /api/sessions/{chat}/browser/frame                      [NEW]
  POST /api/sessions/{chat}/browser/open | /close              [NEW]
        |
Agent loop (harness/turn.py -> harness/tools.py)
  browser_* tools -> execute_tool -> path rules (_ws_path) + origin approval gate
                                     + decision ledger (decisions.py)
        |
Harness browser glue (harness/browser/ — Python, NEW)
  Supervisor: lazy start, idle teardown, per-chat state.json, frame file, idle timer
  Per-chat state: data/sessions/<chat_id>/browser/{state.json, frame.png, profile/}
        |
Driver layer (ONE tool surface, TWO backends)
  A "ego"  — macOS reference driver: wraps ego-browser CLI (Node ESM scripts),
             disposeEgoSdk teardown, ~/.agents/skills discovery
  B "cdp"  — THIS machine (Linux/Wayland), works today: Go sidecar mdock-cdp
             - CDP WebSocket client (async/network)
             - Chromium process supervision: spawn with CPU flags, per-chat --user-data-dir,
               wait DevToolsActivePort, kill
             - frame capture loop -> per-chat frame file
             - local HTTP control API on 127.0.0.1:<port>
        |
Chromium (153, --headless=new, --use-gl=swiftshader --enable-unsafe-swiftshader,
         per-chat profile dir; NO OS window; NO GPU)
```

Data planes:
- LLM plane: accessibility snapshots with @N refs (text, token-budgeted) — screenshots ONLY
  when visually needed (per user requirement).
- UI plane: frame.png (JPEG/PNG, <=1 fps) polled while the pane is visible; capture pauses when
  the pane is hidden.
- Isolation: one browser space/tab set per chat; per-chat profile dir (CDP driver) or per-chat
  Space with own imported Chrome profile (ego driver).

## 2. Ego-Lite Components

Reused (documented public interface, no patching of app bundle — per docs/local-runtime-development.md):
- ego-browser CLI + public API as the canonical LLM tool surface (spaces, pages, snapshot/@N refs,
  waits, actions, CDP escape, dialogs, file chooser).
- Spaces model: one Space per chat; own Chrome profile per Space; user tabs protected;
  finish({keep}) closes agent pages.
- Snapshot/@N ref semantics — replicated deterministically in the Rust core for the CDP driver so
  both drivers present an IDENTICAL tool surface to the LLM.
- disposeEgoSdk lifecycle discipline for the long-lived supervisor script (ego driver).

NOT reused / not depended on:
- The ego (lite) GUI window as the live view (user constraint: view must be inside ModelDock).
- App onboarding / profile-import wizard (interactive, unscriptable — unverified); CDP driver uses
  its own per-chat profile instead.
- [ego-browser:notice] upgrade flow — harness-managed instead.
- The macOS app on this machine (unavailable; driver A is reference implementation, activated on Mac).

## 3. Required Changes

New files:
- docs/ego-lite-integration-plan.md (this file)
- rust/browser-core/ — Cargo.toml, src/lib.rs, src/refs.rs (ref registry), src/snapshot.rs
  (compaction + token budget), src/locators.rs (stable locators), src/frame_hash.rs (frame-change
  detection), tests/
- go/cdpgate/ — go.mod, main.go, cdp.go (WebSocket CDP client), chrome.go (process supervision),
  frames.go (capture loop), api.go (local HTTP control API)
- harness/browser/__init__.py, supervisor.py, state.py, driver_ego.py, driver_cdp.py
- tests/browser_test.py

Changed files:
- harness/tools.py — register browser_* tool schemas; dispatch in _execute_builtin; reuse
  _ws_path/allow_outside for path args; origin approval gate; dec.record(kind="browser_action")
- harness/turn.py — per-turn system-prompt line with current browser state (existing
  fallback_system_note pattern)
- harness/main.py — 4 new routes (browser status/frame/open/close)
- harness/config.py — browser.* settings (enabled, driver auto|ego|cdp, idle_timeout_s=300,
  frame_fps=1, viewport 1280x720, cpu_only=true, keep_profile=true, default_origins=[])
- harness/decisions.py — new kind "browser_action" through the existing dual JSONL ledgers
- web/index.html — browser pane markup inside tab-chat
- web/css/app.css — pane layout (grid: chat column | browser pane)
- web/js/app.js — frame polling (500ms), visibility pause, status chip, open/close buttons

Unchanged:
- gui.py (same web assets in pywebview; CPU compositing already forced via
  WEBKIT_DISABLE_COMPOSITING_MODE=1)
- harness/chat.py (session layout reused; browser state lives under the harness data dir)
- harness/jj.py (the earlier jj workstream stays queued, separate from this one)

LLM tool surface (identical for both drivers, modeled on ego-browser v2, compact):
| Tool | Args | Notes |
|---|---|---|
| browser_open | url | Starts chat's browser if stopped (lazy), navigates to url |
| browser_navigate | url, waitUntil? | goto current page (commit/domcontentloaded/load/networkidle) |
| browser_snapshot | scope? (viewport default), includeStableLocator? | a11y tree with @N refs; Rust-core token budget (default 24k chars) |
| browser_click / browser_type / browser_scroll / browser_press / browser_hover / browser_drag | ref or selector (css=, loc=, xpath, :has-text, >> nth=N) | ego-style ref-or-selector resolution |
| browser_screenshot | path? | pixels ONLY when visually needed; path via _ws_path (workspace-confined, outside => approval) |
| browser_evaluate | js | in-page JS; ledger-recorded |
| browser_cdp | method, params | raw CDP escape — ALWAYS approval-gated |
| browser_status | — | driver, url, title, pages, ref count, cpu-only badge |
| browser_close | keep? | close agent tabs/space; triggers supervisor teardown |

Sandbox / approval integration:
- Path args (screenshot path, fileChooser uploads, download dir) resolve through _ws_path;
  outside workspace => existing approval dict flow.
- Origin gate: open/navigate to a NEW origin not in the chat's allow-list => "approval" decision
  (approvals.py + POST /approve); allow-list per chat (config browser.default_origins, default:
  ask).
- browser_cdp: always approval (raw browser control).
- Every action: dec.record(chat_id, "browser_action", {tool, url, ref, ok}) -> visible in
  Decisions tab.

## 4. Browser UI

Web frontend (web/):
- tab-chat becomes a two-column grid: [chat column | browser pane]; pane hidden until a browser
  is active for this chat (CSS class toggle).
- Pane contents: <img id="browser-frame"> (polled), status row (driver chip cdp|ego, CPU-only
  badge, url, page label), buttons Open/Close, manual Snapshot, frame FPS select (0.5/1/2).
- Polling: GET /api/sessions/{chat}/browser/frame?ts=<lastTs> every 500ms; only while the chat
  tab is visible (visibilitychange + tab switch); paused => sidecar stops frame capture after
  ~2s without requests (idle capture, not idle teardown).

Native pywebview (gui.py):
- gui.py unchanged: it serves the same web/ assets into the existing WebKit2GTK window
  (1180x760); the pane is the same DOM element.
- WebKit already runs CPU-composited (WEBKIT_DISABLE_COMPOSITING_MODE=1, GSETTINGS_BACKEND=memory).
- Constraint: NO separate OS window. CDP driver runs Chromium with --headless=new => no window at
  all. (ego driver on macOS: app window exists; minimized; no documented headless mode — see
  Risks.)

## 5. Resource Optimization

Lazy lifecycle:
- No browser process exists until the first browser_* call or the user clicks Open.
- Idle teardown: no actions AND no frame requests for browser.idle_timeout_s (default 300) =>
  supervisor stops the driver; Chromium killed => RAM returned to OS. Explicit browser_close /
  UI button as well.
- One space/tab set per chat; reuse via goto (no tab sprawl).

Data plane:
- Snapshot-first (Ego-Lite's core efficiency: compressed a11y text with @N refs, not pixels);
  screenshots only when visually needed.
- Rust-core token budget caps snapshot size (default 24k chars) with a truncation marker.

Frame plane:
- <=1 fps default, single frame file overwritten (zero disk growth), JPEG q80, viewport
  1280x720 default.
- Capture pauses while pane hidden (no frame request >2s => capture loop idles).

CPU-only / RAM (user constraint, verbatim: "make sure to force the browser to run on ram and cpu
and not let it touch gpu. Since it uses a lot of ram."):
- CDP driver (this machine, Chromium 153 on Wayland):
  launch: chromium --headless=new --use-gl=swiftshader --enable-unsafe-swiftshader
          --ozone-platform=wayland --user-data-dir=data/sessions/<chat>/browser/profile
          --window-size=1280,720
  - SwiftShader = bundled CPU-only Vulkan/GL implementation => no GPU process.
  - --headless=new => no OS window, no compositor load on the Wayland session.
  - Enforced + verified at startup (tests/browser_test.py): about:gpu dump must show software
    rendering; GPU counters (nvidia-smi or equivalent) must show no chromium process; test
    fails otherwise.
- pywebview GUI: already CPU-composited.
- ego driver (macOS): no flag injection into the prebuilt app (unverified); mitigations: small
  viewport, <=1 fps, idle teardown; documented limitation.

RAM:
- Headless Chromium ~100-300 MB per active chat; no warm instances; per-chat profile dirs are
  independent; profile deleted on chat deletion (keep_profile=true keeps a chat's profile between
  its own sessions).
- Frames never accumulate on disk; state.json is tiny.

## 6. Language split + bacon/tmux workflow

- Rust `browser-core` (deterministic, minimal memory): ref registry (stable @N <-> node identity),
  snapshot compaction + token budget, stable-locator computation (role/name, :has-text, nth),
  frame-change hash (skip unchanged frames), state JSON validation. Pure functions, no I/O,
  unit-tested.
- Go `mdock-cdp` (async/network): CDP WebSocket client (Target/Page/Runtime/DOM/
  Accessibility/Input domains), Chromium process supervision (spawn, DevToolsActivePort wait,
  kill), frame capture loop, local HTTP control API (127.0.0.1, JSON).
- Python (glue): harness/browser/ supervisor + state + tool wiring + approvals/decisions +
  FastAPI routes + config.
- tmux + bacon (per user):
  - tmux new-session -d -s bacon-browser-core -c modeldock/rust/browser-core
    bacon -w 100   (detached per Rust dir)
  - After every Rust edit: tmux capture-pane -t bacon-browser-core -p | tail -5 — compile ONLY
    when bacon says ready; then cargo test.
  - Kill the session when the Rust workstream ends.

## 7. Risks / Unknowns

1. Ego-Lite app is macOS-only (verified) => on this machine only Driver B (cdp) runs; Driver A
   (ego) is the reference implementation for Mac. If the Ego-Lite-based integration (tool surface +
   driver layer) fails end-to-end on this machine => roll back via jj to ego-lite/plan and re-plan
   around https://github.com/vercel-labs/agent-browser (same UI + tool surface, different driver).
2. No remote-debugging port in the ego app by design (verified) => live view only through the SDK
   (page.screenshot / page.cdp / page.events); whether Page.startScreencast frame events surface
   via page.events() is UNVERIFIED => fallback: page.screenshot polling.
3. UNVERIFIED: headless/off-screen mode for the ego app (no docs found).
4. UNVERIFIED: flag injection into the ego app binary (CPU forcing) — cannot be tested on this
   machine.
5. Long-lived supervisor Node script (ego driver): SDK event buffer 10k cap; minutes-long-lived
   script lifecycle unverified => heartbeat + periodic respawn + disposeEgoSdk on exit.
6. ego app first-run profile import is an interactive wizard (unscriptable, unverified) => CDP
   driver bypasses it with its own per-chat profile.
7. SwiftShader-on-Wayland: flags documented in community sources; MUST be verified on this machine
   (Stage 1, before driver code).
8. LLM token cost per snapshot — inherent; mitigated by compaction + viewport scope + budgets.
9. Chromium sandboxing: headless shell may require --no-sandbox in some environments; supervisor
   detects and reports (documented security tradeoff).
10. jj: all browser artifacts (frame, profile, state) live under data/ (gitignored) => no
    snapshot impact; .gitignore already covers it.

## 8. Implementation Order (jj-tracked, one bookmark per stage)

- Stage 0 (done): research + this plan; jj git init; .gitignore; bookmark ego-lite/plan.
- Stage 1 — Chromium CPU verification on this box: launch with SwiftShader flags; assert software
  GL via --dump-dom about:gpu; assert no GPU process; encode as tests/browser_test.py.
- Stage 2 — Go mdock-cdp: CDP client + Chromium supervision + frame loop + HTTP control API;
  e2e: local fixture page -> navigate -> snapshot -> click -> frame.
- Stage 3 — Rust browser-core: refs/snapshot/locators/frame_hash + cargo test (bacon tmux session).
- Stage 4 — Python glue: harness/browser/, tools.py entries + approval/decision wiring,
  main.py routes, config.py, system-prompt note.
- Stage 5 — Web UI: pane markup/CSS/polling; verify in run.py (127.0.0.1:8787) AND gui.py.
- Stage 6 — macOS ego driver (driver_ego): heredoc builder, supervisor script, disposeEgoSdk
  teardown, ~/.agents/skills discovery (code-complete; manual Mac test unavailable here).
- Stage 7 — Settings tab browser section + README section.
- Stage 8 — Test suite: CPU assertion, idle teardown (process gone), approval gate, frame flow,
  snapshot->click round trip; bookmark ego-lite/complete.

jj discipline: jj describe + bookmark per stage: ego-lite/plan, ego-lite/chrome-cpu,
ego-lite/cdp-driver, ego-lite/rust-core, ego-lite/glue, ego-lite/ui, ego-lite/ego-driver,
ego-lite/complete. Revert = jj workspace update --to <bookmark>. Working state never destroyed.
Fallback trigger: if Stage 2 or 4 fails end-to-end on this machine => revert to ego-lite/plan and
write the agent-browser (vercel-labs) plan (user instruction).

## 9. Open questions (resolved during implementation)

- Does Page.startScreencast work over a plain CDP WebSocket (Go driver uses it directly — the
  uncertainty only matters for the ego app path)?
- Exact --no-sandbox requirement in this environment.
- ego app macOS install path for driver_ego manual testing (no Mac on this box).
