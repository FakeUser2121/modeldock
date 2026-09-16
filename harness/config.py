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
}


def _ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def load() -> dict:
    """Load config, creating defaults on first run.

    Cached by (mtime, size): a hit avoids the file read + JSON parse on
    every turn/tool call. Callers must not mutate the returned dict in
    place without calling `save()` (all current callers read-only or
    save a fresh dict).
    """
    with _lock:
        _ensure_dirs()
        p = DATA_DIR / "config.json"
        if not p.exists():
            cfg = json.loads(json.dumps(DEFAULT_CONFIG))
            p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            _set_cache(cfg, None)
            return cfg
        try:
            st = p.stat()
        except OSError:
            st = None
        key = (st.st_mtime_ns, st.st_size) if st else None
        if _cache is not None and key is not None and key == _cache_key:
            return _cache
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cfg = json.loads(json.dumps(DEFAULT_CONFIG))
            p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            st = p.stat()
            key = (st.st_mtime_ns, st.st_size)
        _set_cache(cfg, key)
        return cfg


def save(cfg: dict) -> None:
    """Persist config atomically."""
    with _lock:
        _ensure_dirs()
        target = DATA_DIR / "config.json"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)
        st = target.stat()
        _set_cache(cfg, (st.st_mtime_ns, st.st_size))
