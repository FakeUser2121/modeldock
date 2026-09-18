"""Chat sessions.

Folder system: every chat lives in the folder the user started it in.
The chosen folder is the machine (local drive) folder the chat works in,
and it is the ONLY folder the LLM is bound to. The chat's own records are
kept inside that folder:

    <workspace>/.modeldock/<chat_id>/
        session.json     meta + local settings (persisted per-chat)
        messages.json    conversation history
        decisions.jsonl  this chat's decision ledger

A small registry (data/registry.json) maps chat id -> folder so sessions
can be listed no matter which local folder they live in. Sessions created
before the folder system stay in data/sessions/<id>/ and keep working
(legacy fallback).

The session is bound to its workspace directory. Everything the agent
does is confined there unless the user approves an outside action (see
sandbox, later step).
"""
import json
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from . import config as cfg
from . import decisions as dec
from . import jj
from . import sandbox
from . import thinking as th
from .config import DATA_DIR, SESSIONS_DIR

REGISTRY_FILE = DATA_DIR / "registry.json"

SETTINGS_KEYS = (
    "server",
    "model",
    "mode",
    "agent_type",
    "temperature",
    "top_p",
    "max_tokens",
    "repeat_penalty",
    "system_prompt",
    "thinking",
)


def _valid_id(chat_id: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", chat_id or ""))


# ---------------- registry (chat id -> the folder it lives in) ----------------

_reg_cache: dict | None = None
_reg_key: tuple | None = None
_reg_lock = threading.Lock()


def _registry() -> dict:
    """chat id -> {title, workspace, created_at}, cached by (mtime, size).

    `session_dir()` calls this, and `session_dir()` sits under nearly every
    read in the harness (history, todos, decisions, browser state), so the
    uncached version meant a stat + read + JSON parse per tool call.
    """
    global _reg_cache, _reg_key
    with _reg_lock:
        try:
            st = REGISTRY_FILE.stat()
        except OSError:
            _reg_cache, _reg_key = {}, None
            return {}
        key = (st.st_mtime_ns, st.st_size)
        if _reg_cache is not None and key == _reg_key:
            return _reg_cache
        try:
            data = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
        _reg_cache = data if isinstance(data, dict) else {}
        _reg_key = key
        return _reg_cache


def _write_registry(reg: dict) -> None:
    global _reg_cache, _reg_key
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(REGISTRY_FILE)
    with _reg_lock:
        _reg_cache = reg
        try:
            st = REGISTRY_FILE.stat()
            _reg_key = (st.st_mtime_ns, st.st_size)
        except OSError:
            _reg_key = None


def _register(chat_id: str, workspace: str, title: str, created_at: float) -> None:
    reg = _registry()
    reg[chat_id] = {"title": title, "workspace": workspace, "created_at": created_at}
    _write_registry(reg)


def _unregister(chat_id: str) -> None:
    reg = _registry()
    if chat_id in reg:
        del reg[chat_id]
        _write_registry(reg)


def session_dir(chat_id: str) -> Path:
    """The chat's own folder, inside the folder it was started in.

    <workspace>/.modeldock/<chat_id>/ for registered chats;
    data/sessions/<chat_id>/ for legacy (pre-folder-system) chats.
    """
    if not _valid_id(chat_id):
        return SESSIONS_DIR / chat_id
    entry = _registry().get(chat_id)
    if entry:
        return Path(entry["workspace"]).expanduser() / ".modeldock" / chat_id
    return SESSIONS_DIR / chat_id


def decisions_path(chat_id: str) -> Path:
    """This chat's decision ledger, inside the chat's folder."""
    return session_dir(chat_id) / "decisions.jsonl"


def _load_meta(chat_id: str) -> dict | None:
    meta = session_dir(chat_id) / "session.json"
    if not meta.exists():
        return None
    try:
        return json.loads(meta.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def list_sessions() -> list:
    out = []
    seen = set()
    for sid in _registry():
        seen.add(sid)
        meta = _load_meta(sid)
        if meta:
            out.append(meta)
    if SESSIONS_DIR.is_dir():
        for d in sorted(SESSIONS_DIR.iterdir(), key=lambda p: p.name):
            if not d.is_dir() or d.name in seen:
                continue
            meta = _load_meta(d.name)
            if meta:
                out.append(meta)
    out.sort(key=lambda m: m.get("created_at", 0), reverse=True)
    return out


def get_session(chat_id: str) -> dict | None:
    if not _valid_id(chat_id):
        return None
    return _load_meta(chat_id)


def _save_session(meta: dict) -> None:
    d = session_dir(meta["id"])
    d.mkdir(parents=True, exist_ok=True)
    target = d / "session.json"
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, indent=1), encoding="utf-8")
    tmp.replace(target)


def create_session(title: str, workspace: str, server: str | None = None, model: str | None = None) -> dict:
    sid = uuid.uuid4().hex[:8]
    ws = Path(workspace or "").expanduser()
    if not ws.is_absolute():
        ws = Path.cwd() / ws
    ws = ws.resolve()
    created = not ws.is_dir()
    ws.mkdir(parents=True, exist_ok=True)
    # A new chat starts from the global defaults (Settings tab -> Global
    # defaults). An explicit server/model passed at creation wins; everything
    # is stored per-chat in this session's meta and is only changed when the
    # user changes it inside that chat.
    # The folder automatically becomes a jj repository (if not already one),
    # so every chat's work in this folder is version-tracked without the
    # LLM ever touching jj.
    try:
        jj.ensure_repo(str(ws))
    except (RuntimeError, OSError):
        pass
    defaults = cfg.load().get("defaults", {})
    settings = {
        "server": server or defaults.get("default_server") or None,
        "model": model or defaults.get("default_model") or None,
        "mode": defaults.get("mode") or "write",
        "agent_type": defaults.get("agent_type") or "general",
        "temperature": defaults.get("temperature"),
        "top_p": defaults.get("top_p"),
        "max_tokens": defaults.get("max_tokens"),
        "repeat_penalty": defaults.get("repeat_penalty"),
        "system_prompt": defaults.get("system_prompt") or "",
        "thinking": defaults.get("thinking") or "off",
    }
    meta = {
        "id": sid,
        "title": (title or "").strip() or "Chat",
        "workspace": str(ws),
        "created_at": time.time(),
        "workspace_created": created,
        "settings": settings,
    }
    # The chat lives in the folder it was started in:
    # <workspace>/.modeldock/<chat_id>/. Register it so it stays findable
    # no matter which local folder it is in.
    _register(sid, str(ws), meta["title"], meta["created_at"])
    _save_session(meta)
    dec.record(
        sid,
        "session_created",
        {"title": meta["title"], "workspace": str(ws), "created": created},
        per_chat_path=decisions_path(sid),
    )
    return meta


def update_session(chat_id: str, patch: dict) -> dict:
    meta = get_session(chat_id)
    if not meta:
        raise ValueError("unknown session")
    changed = {}
    if "title" in patch and patch["title"] != meta.get("title"):
        meta["title"] = (patch["title"] or "").strip() or meta["title"]
        changed["title"] = meta["title"]
    old_dir = session_dir(chat_id)
    if "workspace" in patch and patch["workspace"] and patch["workspace"] != meta.get("workspace"):
        ws = Path(patch["workspace"]).expanduser().resolve()
        if not ws.is_dir():
            raise ValueError(f"workspace does not exist: {ws}")
        meta["workspace"] = str(ws)
        changed["workspace"] = str(ws)
        # The sidecar's profile lives under the old folder; stop it before
        # the folder moves so it does not keep writing to a stale path.
        _close_browser(chat_id)
        # Re-home the chat in the new folder and move its records with it.
        _register(chat_id, str(ws), meta.get("title", "Chat"), meta.get("created_at", 0.0))
        new_dir = session_dir(chat_id)
        if old_dir != new_dir:
            new_dir.parent.mkdir(parents=True, exist_ok=True)
            if old_dir.exists():
                shutil.move(str(old_dir), str(new_dir))
        try:
            jj.ensure_repo(str(ws))
        except (RuntimeError, OSError):
            pass
    settings = meta.setdefault("settings", {})
    for k in SETTINGS_KEYS:
        if k in patch:
            if k == "mode" and patch[k] is not None and patch[k] not in sandbox.MODES:
                raise ValueError(f"unknown mode: {patch[k]!r} (use one of {sandbox.MODES})")
            if k == "thinking" and patch[k] is not None and patch[k] not in th.THINKING_LEVELS:
                raise ValueError(f"unknown thinking level: {patch[k]!r} (use one of {th.THINKING_LEVELS})")
            settings[k] = patch[k]
            changed[k] = patch[k]
    _save_session(meta)
    if changed:
        dec.record(chat_id, "session_updated", changed, per_chat_path=decisions_path(chat_id))
    return meta


def bump_usage(chat_id: str, total_tokens: int) -> dict | None:
    """Add one turn's token count to the session's running usage totals."""
    meta = get_session(chat_id)
    if not meta:
        return None
    u = meta.get("usage") or {"turns": 0, "total_tokens": 0}
    u["turns"] = u.get("turns", 0) + 1
    u["total_tokens"] = u.get("total_tokens", 0) + int(total_tokens or 0)
    meta["usage"] = u
    _save_session(meta)
    return u


def delete_session(chat_id: str) -> None:
    if not get_session(chat_id):
        raise ValueError("unknown session")
    _close_browser(chat_id)
    shutil.rmtree(session_dir(chat_id), ignore_errors=True)
    _unregister(chat_id)
    dec.record(None, "session_removed", {"chat_id": chat_id})


def _close_browser(chat_id: str) -> None:
    """Stop this chat's Chromium, if one is running.

    Deleting the session folder (or moving the workspace) out from under a
    live sidecar leaves an orphaned browser holding a profile directory that
    no longer exists, and nothing left in the registry can reach it.
    """
    try:
        from .browser import close as browser_close
    except ImportError:
        return
    try:
        browser_close(chat_id)
    except Exception:
        pass


def fork_session(chat_id: str, title: str = "", up_to_index: int | None = None) -> dict:
    """Create a new session in the same workspace with the same settings and
    a copy of the history. The fork is independent from here on.

    `up_to_index` (0-based index into the history) forks from that message:
    the fork contains the history up to and including that message, so the
    user can rework any earlier branch. None = full history."""
    meta = get_session(chat_id)
    if not meta:
        raise ValueError("unknown session")
    history = get_history(chat_id)
    if up_to_index is not None:
        if not history:
            raise ValueError("cannot fork from a message: chat has no history")
        idx = int(up_to_index)
        if idx < 0 or idx >= len(history):
            raise ValueError(f"fork index {idx} out of range (history has {len(history)} messages)")
        history = history[: idx + 1]
    default_title = f"{meta.get('title', 'Chat')} (fork)"
    if up_to_index is not None:
        default_title += f" from msg {int(up_to_index) + 1}"
    new = create_session(title or default_title, meta.get("workspace") or "")
    if history:
        save_history(new["id"], [dict(m) for m in history])
    settings = {k: v for k, v in (meta.get("settings") or {}).items() if k in SETTINGS_KEYS}
    if settings:
        new = update_session(new["id"], settings)
    dec.record(
        new["id"],
        "session_forked",
        {"from": chat_id, "messages": len(history), "up_to_index": up_to_index, "workspace": meta.get("workspace")},
        per_chat_path=decisions_path(new["id"]),
    )
    return new


# ---------------- history (used from step 4 on) ----------------

def get_history(chat_id: str) -> list:
    if not _valid_id(chat_id):
        return []
    f = session_dir(chat_id) / "messages.json"
    if not f.exists():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def save_history(chat_id: str, messages: list) -> None:
    """Persist the conversation atomically.

    The folder is created if it is missing: returning silently (the old
    behaviour) threw away the turn whenever the session folder had not been
    materialised yet, or had been moved out from under a running turn.
    """
    if not _valid_id(chat_id):
        return
    d = session_dir(chat_id)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    f = d / "messages.json"
    tmp = f.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(messages, indent=1), encoding="utf-8")
        tmp.replace(f)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
