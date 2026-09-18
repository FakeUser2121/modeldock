"""Global configuration store.

Everything the user configures lives here and is persisted as JSON, so
nothing ever has to be entered twice:
- servers            (url, api key, context size, description)
- defaults           (global chat defaults: params, mode, agent type)
- agent_types        (editable personas/prompts)
- allowed_outside_commands (whitelist for autonomous mode)
- mcp_servers        (MCP server registry)

Per-chat settings live in each session's meta.json (see chat.py) — those are
the "local" settings; this file is the "global" one.
"""
import copy
import json
import os
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("MODELDOCK_DATA", ROOT / "data"))
SESSIONS_DIR = DATA_DIR / "sessions"
GLOBAL_LEDGER = DATA_DIR / "decisions-global.jsonl"

_lock = threading.Lock()

# Small mtime/size cache so the hot path (every turn, every tool call)
# does not re-read and re-parse config.json. `save()` invalidates it.
_cache: dict | None = None
_cache_key: tuple | None = None


def _set_cache(cfg: dict, key: tuple | None) -> None:
    global _cache, _cache_key
    _cache = cfg
    _cache_key = key


def _merge_defaults(cur: dict, defaults: dict) -> tuple[dict, bool]:
    """Fill in keys the stored config is missing, without touching set ones.

    An install created before a key existed (e.g. `browser`, or `agent_types`
    on a config written by an older build) otherwise reads back as an empty
    section forever: `load().get("agent_types", [])` returns [] and every
    chat silently loses its persona prompt. Only absent keys are added --
    a user's own value, including an empty list they chose, is preserved
    verbatim. Returns (merged, changed).
    """
    changed = False
    out = dict(cur)
    for k, dv in defaults.items():
        if k not in out:
            out[k] = copy.deepcopy(dv)
            changed = True
        elif isinstance(dv, dict) and isinstance(out.get(k), dict):
            sub, sub_changed = _merge_defaults(out[k], dv)
            if sub_changed:
                out[k] = sub
                changed = True
    return out, changed

DEFAULT_CONFIG = {
    "servers": [],
    # {id, name, url, api_key, adapter, context_size, description, model_context:{model_id: n}}
    "defaults": {
        "default_server": None,  # server id every new chat starts with (until changed)
        "default_model": None,  # model every new chat starts with (until changed)
        "temperature": 0.7,
        "top_p": 1.0,
        "max_tokens": 2048,
        "repeat_penalty": None,
        "mode": "write",  # readonly | write | autonomous | extend
        "thinking": "off",  # off | low | medium | high | xhigh (per-model fallback applies)
        "agent_type": "general",
        "system_prompt": "",
    },
    "agent_types": [
        {
            "id": "general",
            "name": "General",
            "prompt": (
                "You are a helpful general-purpose assistant. Answer directly and concisely. "
                "When tools are available and they would help (reading files, running commands), use them."
            ),
        },
        {
            "id": "coder",
            "name": "Coder",
            "prompt": (
                "You are a careful software engineer. Work only inside the workspace directory. "
                "Read before you edit. Prefer minimal, well-explained changes. "
                "Verify your work by running commands when possible."
            ),
        },
        {
            "id": "planner",
            "name": "Planner",
            "prompt": (
                "You are a planning agent. Break tasks into concrete steps, state assumptions, "
                "risks, and next actions. Do not modify files unless asked."
            ),
        },
        {
            "id": "reviewer",
            "name": "Reviewer",
            "prompt": (
                "You are a code reviewer. Read the relevant files, list concrete issues with "
                "file:line references, and suggest minimal fixes. Do not apply changes unless asked."
            ),
        },
        {
            "id": "researcher",
            "name": "Researcher",
            "prompt": (
                "You are a research assistant. Gather and synthesize information, cite sources "
                "where possible, and mark your confidence per claim."
            ),
        },
    ],
    "allowed_outside_commands": [
        "python3 --version",
        "which python3",
        "git --version",
    ],
    "mcp_servers": [],
    # {id, name, transport: stdio|http, command, args, env, url, enabled}
    # browser subsystem (Ego-Lite-style CDP tools; docs/ego-lite-integration-plan.md)
    "browser": {
        "enabled": True,
        "idle_timeout_s": 300,  # tear down an idle per-chat browser after this long
        "frame_fps": 1,          # live pane cadence (sidecar already throttles frames to 1/s)
        "viewport": "1280x720",  # headful window size (software-GL rendered, CPU-only)
        "cpu_only": True,        # enforce software GL (SwiftShader); never touch the GPU
        "default_origins": [],   # origins needing no approval for navigation
    },
}


def _ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def load() -> dict:
    """Load config, creating defaults on first run.

    Cached by (mtime, size): a hit avoids the file read + JSON parse on
    every turn/tool call. Missing sections are back-filled from
    DEFAULT_CONFIG (see `_merge_defaults`) and written back once, so an
    older config.json gains new keys instead of reading as empty.

    The caller gets its own copy: the cache is shared across threads and
    a caller that appends to e.g. `cfg["mcp_servers"]` would otherwise
    mutate every other reader's view (and the cache) in place.
    """
    with _lock:
        _ensure_dirs()
        p = DATA_DIR / "config.json"
        if not p.exists():
            cfg = copy.deepcopy(DEFAULT_CONFIG)
            _write(p, cfg)
            try:
                st = p.stat()
                _set_cache(cfg, (st.st_mtime_ns, st.st_size))
            except OSError:
                _set_cache(cfg, None)
            return copy.deepcopy(cfg)
        try:
            st = p.stat()
        except OSError:
            st = None
        key = (st.st_mtime_ns, st.st_size) if st else None
        if _cache is not None and key is not None and key == _cache_key:
            return copy.deepcopy(_cache)
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(cfg, dict):
                raise json.JSONDecodeError("config root is not an object", "", 0)
        except (json.JSONDecodeError, OSError):
            # Keep the unreadable file instead of silently destroying it;
            # the user may want to repair it by hand.
            _backup(p)
            cfg = copy.deepcopy(DEFAULT_CONFIG)
            _write(p, cfg)
        else:
            cfg, changed = _merge_defaults(cfg, DEFAULT_CONFIG)
            if changed:
                _write(p, cfg)
        try:
            st = p.stat()
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            key = None
        _set_cache(cfg, key)
        return copy.deepcopy(cfg)


def _write(target: Path, cfg: dict) -> None:
    """Atomic write (same-directory temp + rename)."""
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)


def _backup(p: Path) -> None:
    try:
        p.replace(p.with_suffix(".corrupt"))
    except OSError:
        pass


def save(cfg: dict) -> None:
    """Persist config atomically."""
    with _lock:
        _ensure_dirs()
        target = DATA_DIR / "config.json"
        if not isinstance(cfg, dict):
            raise ValueError("config must be a JSON object")
        cfg, _ = _merge_defaults(cfg, DEFAULT_CONFIG)
        _write(target, cfg)
        # Cache a private copy: the caller keeps its own dict and may go on
        # mutating it after saving.
        snapshot = copy.deepcopy(cfg)
        try:
            st = target.stat()
            _set_cache(snapshot, (st.st_mtime_ns, st.st_size))
        except OSError:
            _set_cache(snapshot, None)
