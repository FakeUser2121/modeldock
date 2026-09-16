"""Custom tool registry: the harness's self-extension mechanism.

A custom tool is a declarative spec with one of two executors:

  command  a shell command template with {param} placeholders. It is run
           through the exact same sandbox/approval gate as run_command, so
           a custom tool can never leave the workspace without consent.
  http     a JSON-over-HTTP request (GET/POST) to a URL template with
           {param} placeholders.

Tools are created by the model (extend_harness meta tool, extend mode) or
directly by the user (Settings UI / API). Every create / update / remove
and every execution is recorded in the decision ledgers, and any tool can
be enabled, disabled, or deleted from the UI.

Persisted in data/extensions.json.
"""
import asyncio
import json
import re
import threading
import uuid

import httpx

from . import decisions as dec
from . import sandbox
from .config import DATA_DIR

_LOCK = threading.Lock()
FILE = DATA_DIR / "extensions.json"

KINDS = ("command", "http")
_NAME_RE = re.compile(r"[a-z][a-z0-9_]*")
_MAX_PARAMS = 8
_MAX_TEMPLATE_LEN = 1000
_MAX_DESCRIPTION = 500
MAX_OUTPUT = 8000
HTTP_TIMEOUT = 15.0

# Built-in names that custom tools may not shadow.
RESERVED_NAMES = {"run_command", "extend_harness"}


class ExtensionError(Exception):
    pass


# ---------------- storage ----------------

def _load_unlocked() -> list:
    """Read the registry. Caller must hold _LOCK."""
    if not FILE.exists():
        return []
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def _save_unlocked(specs: list) -> None:
    """Atomically write the registry. Caller must hold _LOCK."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(specs, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(FILE)


def _load() -> list:
    with _LOCK:
        return _load_unlocked()


def _save(specs: list) -> None:
    with _LOCK:
        _save_unlocked(specs)


def list_tools() -> list:
    return _load()


def get_by_id(tool_id: str) -> dict | None:
    for t in _load():
        if t.get("id") == tool_id:
            return t
    return None


def get_by_name(name: str) -> dict | None:
    for t in _load():
        if t.get("name") == name:
            return t
    return None


# ---------------- spec validation ----------------

def _validate_common(name: str, kind: str, params: list, description: str,
                     ignore_id: str | None = None) -> None:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name.strip()):
        raise ExtensionError(f"tool name must be lowercase [a-z][a-z0-9_]*: {name!r}")
    name = name.strip()
    if name in RESERVED_NAMES:
        raise ExtensionError(f"tool name {name!r} is reserved")
    if name.startswith("mcp__"):
        raise ExtensionError(f"tool name {name!r} collides with MCP naming")
    existing = get_by_name(name)
    if existing is not None and existing.get("id") != ignore_id:
        raise ExtensionError(f"tool name {name!r} already exists")
    if kind not in KINDS:
        raise ExtensionError(f"kind must be one of {KINDS}")
    if not isinstance(params, list) or not 1 <= len(params) <= _MAX_PARAMS:
        raise ExtensionError(f"params must be a list of 1..{_MAX_PARAMS} names")
    seen = set()
    for p in params:
        if not isinstance(p, str) or not _NAME_RE.fullmatch(p.strip()):
            raise ExtensionError(f"invalid param name: {p!r}")
        p = p.strip()
        if p in seen:
            raise ExtensionError(f"duplicate param {p!r}")
        seen.add(p)
    if description is not None and not isinstance(description, str):
        raise ExtensionError("description must be a string")
    description = (description or "").strip()[:_MAX_DESCRIPTION]


def _placeholders(template: str) -> set:
    out = set()
    i = 0
    while i < len(template):
        if template[i] == "{":
            close = template.find("}", i)
            if close == -1:
                raise ExtensionError("unbalanced '{' in template")
            inner = template[i + 1 : close].strip()
            if not _NAME_RE.fullmatch(inner):
                raise ExtensionError(f"invalid placeholder {{{inner}}} in template")
            out.add(inner)
            i = close + 1
        else:
            i += 1
    return out


def validate_spec(spec: dict, ignore_id: str | None = None) -> dict:
    """Validate a raw spec dict and return a normalized copy.

    Raises ExtensionError with a human-readable reason. When updating an
    existing tool, pass ignore_id=that tool's id so its own name does not
    collide with itself.
    """
    if not isinstance(spec, dict):
        raise ExtensionError("spec must be a JSON object")
    name = (spec.get("name") or "")
    kind = (spec.get("kind") or "")
    params = spec.get("params")
    description = spec.get("description")
    _validate_common(name, kind, params, description, ignore_id=ignore_id)
    params = [p.strip() for p in params]

    out = {
        "name": name.strip(),
        "kind": kind,
        "params": params,
        "description": (description or "").strip()[:_MAX_DESCRIPTION],
    }
    if kind == "command":
        template = (spec.get("template") or "").strip()
        if not template:
            raise ExtensionError("command tools need a template")
        if len(template) > _MAX_TEMPLATE_LEN:
            raise ExtensionError("template too long")
        missing = _placeholders(template) - set(params)
        if missing:
            raise ExtensionError(f"template uses placeholders not in params: {sorted(missing)}")
        out["template"] = template
    else:
        url = (spec.get("url") or spec.get("template") or "").strip()
        if not url or not url.lower().startswith(("http://", "https://")):
            raise ExtensionError("http tools need a url starting with http(s)://")
        if len(url) > _MAX_TEMPLATE_LEN:
            raise ExtensionError("url too long")
        missing = _placeholders(url) - set(params)
        if missing:
            raise ExtensionError(f"url uses placeholders not in params: {sorted(missing)}")
        out["url"] = url
        method = (spec.get("method") or "GET").strip().upper()
        if method not in ("GET", "POST"):
            raise ExtensionError("method must be GET or POST")
        out["method"] = method
    return out


def add_tool(spec: dict, source: str) -> dict:
    normalized = validate_spec(spec)
    normalized["id"] = uuid.uuid4().hex[:8]
    normalized["source"] = source
    normalized["enabled"] = True
    with _LOCK:
        specs = _load_unlocked()
        specs.append(normalized)
        _save_unlocked(specs)
    return normalized


def replace_spec(tool_id: str, normalized: dict) -> dict:
    """Replace a stored spec by id with an already-validated normalized spec."""
    with _LOCK:
        specs = _load_unlocked()
        for i, t in enumerate(specs):
            if t.get("id") == tool_id:
                normalized = dict(normalized)
                normalized["id"] = tool_id
                specs[i] = normalized
                _save_unlocked(specs)
                return normalized
    raise ExtensionError("unknown tool")


def set_enabled(tool_id: str, enabled: bool) -> dict:
    with _LOCK:
        specs = _load_unlocked()
        for t in specs:
            if t.get("id") == tool_id:
                t["enabled"] = bool(enabled)
                _save_unlocked(specs)
                return t
    raise ExtensionError("unknown tool")


def remove_tool(tool_id: str) -> dict:
    with _LOCK:
        specs = _load_unlocked()
        kept = []
        removed = None
        for t in specs:
            if t.get("id") == tool_id:
                removed = t
            else:
                kept.append(t)
        if removed is None:
            raise ExtensionError("unknown tool")
        _save_unlocked(kept)
    return removed


# ---------------- OpenAI schemas ----------------

def tool_schema(spec: dict) -> dict:
    props = {p: {"type": "string", "description": f"Value for {{{p}}}" } for p in spec.get("params", [])}
    return {
        "type": "function",
        "function": {
            "name": spec["name"],
            "description": (
                f"{spec.get('description') or spec['name']}. "
                f"Custom tool (kind: {spec['kind']}); params: {', '.join(spec.get('params', []))}."
            ),
            "parameters": {
                "type": "object",
                "properties": props,
                "required": list(props.keys()),
            },
        },
    }


def tool_schemas(enabled_only: bool = True) -> list:
    return [tool_schema(t) for t in _load() if (not enabled_only or t.get("enabled"))]


EXTEND_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "extend_harness",
        "description": (
            "Register a new custom tool into the harness so the agent (in this and "
            "later chats) can use it. Use it when the user asks for a new capability. "
            "kind 'command': a shell command template with {param} placeholders, run "
            "inside the sandbox. kind 'http': a GET/POST request to a URL template. "
            "Every created tool is recorded in the decision log and can be disabled "
            "or removed by the user."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Lowercase tool name, e.g. fetch_url"},
                "description": {"type": "string", "description": "What the tool does, in one sentence."},
                "kind": {"type": "string", "enum": ["command", "http"]},
                "template": {"type": "string", "description": "Command template (kind=command), e.g. 'curl -s {url}'"},
                "url": {"type": "string", "description": "URL template (kind=http), e.g. 'http://api.local/{path}'"},
                "method": {"type": "string", "enum": ["GET", "POST"], "description": "HTTP method (kind=http), default GET"},
                "params": {"type": "array", "items": {"type": "string"}, "description": "Placeholder names used in the template/url."},
            },
            "required": ["name", "kind", "params"],
        },
    },
}


# ---------------- execution ----------------

def _substitute(template: str, args: dict) -> str:
    out = template
    for p in re.findall(r"\{([a-z][a-z0-9_]*)\}", template):
        if p not in args:
            raise ExtensionError(f"missing value for parameter {p!r}")
        out = out.replace("{" + p + "}", str(args[p]))
    return out


async def execute(spec: dict, args: dict, session: dict) -> dict:
    """Execute a custom tool. Same result vocabulary as run_command,
    including `decision: approval` so the turn can suspend for the user."""
    name = spec.get("name", "")
    kind = spec.get("kind")
    settings = session.get("settings", {})
    mode = settings.get("mode") or "write"
    workspace = session.get("workspace") or ""

    try:
        if kind == "command":
            cmd = _substitute(spec["template"], args)
        elif kind == "http":
            url = _substitute(spec["url"], args)
            method = spec.get("method", "GET")
        else:
            return {"ok": False, "error": f"unknown tool kind: {kind}"}
    except ExtensionError as e:
        return {"ok": False, "error": str(e)}

    if kind == "command":
        if not workspace:
            return {"ok": False, "error": "session has no workspace"}
        whitelist = None
        try:
            from . import config as cfg

            whitelist = cfg.load().get("allowed_outside_commands", [])
        except Exception:  # noqa: BLE001
            whitelist = []
        try:
            decision = sandbox.decide(mode, cmd, workspace, whitelist)
        except sandbox.SandboxError as e:
            return {"ok": False, "error": f"sandbox: {e}"}
        if decision["action"] == "deny":
            return {"decision": "deny", "ok": False, "error": decision.get("reason", "denied by sandbox")}
        if decision["action"] == "approval":
            return {
                "decision": "approval",
                "ok": False,
                "approval": {
                    "cmd": cmd,
                    "reason": f"custom tool {name}: {cmd}",
                    "paths_outside": decision.get("paths_outside", []),
                    "mode": mode,
                },
            }
        result = await asyncio.to_thread(sandbox.run_command, cmd, workspace)
        return {
            "decision": "allow",
            "ok": result["ok"],
            "exit": result["exit"],
            "stdout": result["stdout"],
            "stderr": result["stderr"],
        }

    # http kind
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        try:
            if method == "GET":
                r = await client.get(url)
            else:
                r = await client.post(url, json=args)
            body = r.text[:MAX_OUTPUT]
            return {
                "decision": "allow",
                "ok": r.status_code < 400,
                "exit": r.status_code,
                "stdout": body,
                "stderr": "" if r.status_code < 400 else f"HTTP {r.status_code}",
            }
        except httpx.HTTPError as e:
            return {"ok": False, "exit": None, "stdout": "", "stderr": f"{type(e).__name__}: {e}"}
