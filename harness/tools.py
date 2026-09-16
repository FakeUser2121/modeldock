"""Tool definitions + sandboxed execution.

Tool schemas are sent to the model as OpenAI-style `tools`; the model's
tool_calls come back through the adapter stream and are executed here.

Tool families:
   run_command      the built-in sandboxed shell tool (all non-readonly modes)
   read_file        built-in file reader (confined to the chat workspace)
   write_file       built-in file writer (outside the workspace -> approval)
   edit_file        exact-match string replace (outside -> approval)
   list_files       directory listing / glob (confined to the workspace)
   todo_add         add a todo to this chat's todo list
   todo_mark        mark a todo done / open again
   mcp__*           tools of enabled MCP servers (not in readonly)
   <custom name>    declarative custom tools registered in harness/extensions.py
   extend_harness   meta tool (extend mode only): registers a new custom tool

Execution is sandboxed (harness/sandbox.py): every action gets an
allow / deny / approval decision. On `approval`, the turn pauses and asks
the user (harness/turn.py + POST /api/sessions/{id}/approve).
"""
import asyncio
import json
import time
from pathlib import Path

from . import chat as ch
from . import config as cfg
from . import decisions as dec
from . import sandbox

MAX_TOOL_ROUNDS = 8
OUTPUT_CAP = 8000

# ---------------------------------------------------------------- schemas

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run a shell command inside this chat's workspace folder. "
                "Use it to inspect files, build things, run scripts, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": "The shell command to run."},
                    "reason": {"type": "string", "description": "Why this command is needed."},
                },
                "required": ["cmd", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a text file from this chat's workspace. Use this instead of "
                "shell commands to view files. Paths are relative to the workspace "
                "root (absolute paths inside the workspace also work). Returns "
                "numbered lines, truncated to 8000 characters; page with offset/limit."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path (relative to the workspace, or absolute inside it)."},
                    "offset": {"type": "integer", "description": "1-based first line to return (default 1)."},
                    "limit": {"type": "integer", "description": "Maximum number of lines to return (default 400, max 2000)."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Create or overwrite a text file in this chat's workspace with the "
                "given exact content. Prefer this over shell heredocs. Parent "
                "directories are created automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path (relative to the workspace, or absolute inside it)."},
                    "content": {"type": "string", "description": "The full file content to write."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace an exact string in a file of this chat's workspace. "
                "old_string must occur exactly once unless replace_all is true."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path (relative to the workspace, or absolute inside it)."},
                    "old_string": {"type": "string", "description": "The exact text to replace."},
                    "new_string": {"type": "string", "description": "The replacement text (empty string to delete)."},
                    "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)."},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "List files and directories in this chat's workspace, optionally "
                "filtered by a glob pattern (e.g. '*.py'). Output capped at 500 entries."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory to list (relative to the workspace; default: workspace root)."},
                    "pattern": {"type": "string", "description": "Optional glob pattern to filter matches (e.g. '*.py')."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_add",
            "description": (
                "Add a todo to this chat's todo list. Use it to plan multi-step work "
                "during a run; the list is re-injected into your prompt periodically so "
                "you cannot lose track of it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The todo text."},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_mark",
            "description": (
                "Mark a todo by its numeric id as done (done=true) or open again "
                "(done=false)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "The todo id (from the todo list)."},
                    "done": {"type": "boolean", "description": "True to mark done, false to reopen."},
                },
                "required": ["id", "done"],
            },
        },
    },
]

FILE_TOOLS = ("read_file", "write_file", "edit_file", "list_files")
TODO_TOOLS = ("todo_add", "todo_mark")
WRITE_FILE_TOOLS = ("write_file", "edit_file", "todo_add", "todo_mark")


def tool_catalog() -> list[tuple[str, str]]:
    """[(name, "arg1, arg2, ...")] for every built-in tool."""
    out = []
    for t in TOOLS:
        f = t["function"]
        props = (f.get("parameters") or {}).get("properties") or {}
        out.append((f["name"], ", ".join(props.keys()) or "(none)"))
    return out


# ------------------------------------------- fallback protocol

# JSON fallback protocol for models without native tool calling:
# the model may emit a single line of the form
#   [[tool:{"name": "<tool>", "args": {...}}]]
# which is executed exactly like a native call to that tool.
# Legacy shape [[tool:{"cmd": "...", "reason": "..."}]] still works.
FALLBACK_MARKER = "[[tool:"


def fallback_system_note(extra_tools: list[str] | None = None) -> str:
    """Dynamic tool-protocol note: lists every tool the model can use,
    so non-native models know the full surface (built-ins + MCP + custom)."""
    names = ", ".join(f"{n} ({p})" for n, p in tool_catalog())
    extra = [n for n in (extra_tools or []) if n]
    extra_s = (
        f" Also available through the same protocol: {', '.join(extra)}."
        if extra
        else ""
    )
    return (
        "TOOL PROTOCOL: you can request any tool by writing ONE line of your reply that is "
        "exactly "
        f'{FALLBACK_MARKER}{{"name": "<tool name>", "args": {{...}}}}]]'
        " (that JSON on the single line, nothing else on the line)."
        f" Tools and their argument keys: {names}."
        + extra_s
        + " Only emit a marker when you genuinely need the tool; never quote it in explanatory text."
    )


def parse_fallback_line(line: str) -> dict | None:
    """Return {"name", "args"} if `line` is a complete fallback marker line."""
    s = line.strip()
    if not s.startswith(FALLBACK_MARKER) or not s.endswith("]]") or len(s) <= len(FALLBACK_MARKER) + 2:
        return None
    payload = s[len(FALLBACK_MARKER):-2].strip()
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("args"), dict) and obj.get("name"):
        return {"name": str(obj["name"]), "args": obj["args"]}
    # legacy run_command shape
    cmd = (obj.get("cmd") or "").strip()
    if not cmd:
        return None
    return {"name": "run_command", "args": {"cmd": cmd, "reason": (obj.get("reason") or "").strip()}}


# ------------------------------------------------------------- todos

def todos_path(chat_id: str) -> Path:
    return ch.session_dir(chat_id) / "todos.json"


def load_todos(chat_id: str) -> list:
    try:
        d = json.loads(todos_path(chat_id).read_text(encoding="utf-8"))
        todos = d.get("todos", [])
        return todos if isinstance(todos, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_todos(chat_id: str, todos: list) -> None:
    p = todos_path(chat_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"next_id": max([t.get("id", 0) for t in todos], default=0) + 1, "todos": todos}
    p.write_text(json.dumps(payload, indent=1), encoding="utf-8")


def render_todo_block(todos: list) -> str:
    """Compact numbered block injected at the top of the prompt."""
    if not todos:
        return ""
    lines = []
    for t in todos:
        mark = "x" if t.get("status") == "done" else " "
        lines.append(f"[{mark}] {t.get('id', '?')}. {t.get('text', '')}")
    return (
        "CURRENT TODO LIST (work through it; call todo_mark with done=true "
        "as soon as an item is finished):\n" + "\n".join(lines)
    )


def _exec_todo_add(args: dict, session: dict) -> dict:
    chat_id = session.get("id")
    text = str(args.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "todo_add: text is required"}
    todos = load_todos(chat_id)
    tid = max([t.get("id", 0) for t in todos], default=0) + 1
    todos.append({"id": tid, "text": text, "status": "open", "ts": time.time()})
    save_todos(chat_id, todos)
    dec.record(chat_id, "todo_added", {"id": tid, "text": text}, per_chat_path=ch.decisions_path(chat_id))
    return {"ok": True, "todos": todos}


def _exec_todo_mark(args: dict, session: dict) -> dict:
    chat_id = session.get("id")
    try:
        tid = int(args.get("id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "todo_mark: id must be an integer"}
    done = bool(args.get("done", True))
    todos = load_todos(chat_id)
    for t in todos:
        if t.get("id") == tid:
            t["status"] = "done" if done else "open"
            t["ts"] = time.time()
            break
    else:
        return {"ok": False, "error": f"todo_mark: no todo with id {tid}"}
    save_todos(chat_id, todos)
    dec.record(chat_id, "todo_marked", {"id": tid, "done": done}, per_chat_path=ch.decisions_path(chat_id))
    return {"ok": True, "todos": todos}


# ------------------------------------------------ file tools (sync)

def _ws_path(path_str, workspace: str, allow_outside: bool = False) -> tuple[Path | None, str | None]:
    """Resolve a model-supplied path against the workspace.

    Returns (resolved_path, error). resolved_path is None when the path is
    missing/unresolvable; error is None when the path is inside the workspace
    (or when allow_outside is set and it merely resolved).
    """
    ws = Path(workspace).resolve()
    p = Path(str(path_str or "").strip())
    if not p.parts:
        return None, "path is empty"
    if not p.is_absolute():
        p = ws / p
    try:
        p = p.expanduser().resolve()
    except (OSError, RuntimeError):
        return None, f"cannot resolve path: {path_str}"
    if not allow_outside and sandbox.is_outside(ws, p):
        return None, f"path {p} is outside this chat's workspace ({ws})"
    return p, None


def _rel(ws: Path, p: Path) -> str:
    try:
        return str(p.relative_to(ws))
    except ValueError:
        return str(p)


def _exec_read_file(args: dict, workspace: str, allow_outside: bool = False) -> dict:
    p, err = _ws_path(args.get("path"), workspace, allow_outside)
    if err:
        return {"ok": False, "error": err}
    if not p.is_file():
        return {"ok": False, "error": f"not a file: {_rel(Path(workspace).resolve(), p)}"}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"ok": False, "error": str(e)}
    lines = text.splitlines()
    try:
        offset = max(1, int(args.get("offset") or 1))
        limit = max(1, min(int(args.get("limit") or 400), 2000))
    except (TypeError, ValueError):
        offset, limit = 1, 400
    start = max(0, offset - 1)
    sel = lines[start : start + limit]
    numbered = "\n".join(f"{i + 1}\t{ln}" for i, ln in enumerate(sel, start=offset - 1))
    truncated_lines = start + len(sel) < len(lines)
    body = numbered[:OUTPUT_CAP]
    res = {
        "ok": True,
        "path": _rel(Path(workspace).resolve(), p),
        "total_lines": len(lines),
        "returned_lines": len(sel),
        "content": body,
    }
    notes = []
    if len(numbered) > OUTPUT_CAP:
        notes.append(f"output truncated at {OUTPUT_CAP} chars; page with offset/limit")
    if truncated_lines:
        notes.append(f"file has {len(lines)} lines; keep offset/limit to page the rest")
    if notes:
        res["note"] = "; ".join(notes)
    return res


def _exec_write_file(args: dict, workspace: str, allow_outside: bool = False) -> dict:
    p, err = _ws_path(args.get("path"), workspace, allow_outside)
    if err:
        return {"ok": False, "error": err}
    if p.is_dir():
        return {"ok": False, "error": f"is a directory, not a file: {_rel(Path(workspace).resolve(), p)}"}
    content = args.get("content")
    if content is None:
        return {"ok": False, "error": "content is required"}
    content = str(content)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "path": _rel(Path(workspace).resolve(), p), "bytes": len(content.encode("utf-8"))}


def _exec_edit_file(args: dict, workspace: str, allow_outside: bool = False) -> dict:
    p, err = _ws_path(args.get("path"), workspace, allow_outside)
    if err:
        return {"ok": False, "error": err}
    if not p.is_file():
        return {"ok": False, "error": f"not a file: {_rel(Path(workspace).resolve(), p)}"}
    old = args.get("old_string")
    if old is None or old == "":
        return {"ok": False, "error": "old_string is required"}
    new = str(args.get("new_string") or "")
    try:
        text = p.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as e:
        return {"ok": False, "error": f"cannot read file: {e}"}
    n = text.count(old)
    if n == 0:
        return {"ok": False, "error": f"old_string not found in {_rel(Path(workspace).resolve(), p)}"}
    if n > 1 and not args.get("replace_all"):
        return {
            "ok": False,
            "error": f"old_string occurs {n} times; make it more specific or set replace_all=true",
        }
    replacements = n if args.get("replace_all") else 1
    text2 = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)
    try:
        p.write_text(text2, encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "path": _rel(Path(workspace).resolve(), p), "replacements": replacements}


def _exec_list_files(args: dict, workspace: str, allow_outside: bool = False) -> dict:
    ws = Path(workspace).resolve()
    base = Path(str(args.get("path") or ".").strip())
    if not base.parts:
        base = ws
    if not base.is_absolute():
        base = ws / base
    try:
        base = base.expanduser().resolve()
    except (OSError, RuntimeError):
        return {"ok": False, "error": f"cannot resolve path: {args.get('path')}"}
    if not allow_outside and sandbox.is_outside(ws, base):
        return {"ok": False, "error": f"path {base} is outside this chat's workspace ({ws})"}
    pattern = str(args.get("pattern") or "").strip()
    cap = 500
    entries: list[dict] = []
    try:
        if pattern:
            for m in sorted(base.glob(pattern), key=lambda x: (x.is_file(), x.name)):
                if len(entries) >= cap:
                    break
                entries.append({"name": _rel(ws, m), "dir": m.is_dir()})
        elif base.is_file():
            entries.append({"name": _rel(ws, base), "dir": False})
        else:
            for m in sorted(base.iterdir(), key=lambda x: (x.is_file(), x.name)):
                if len(entries) >= cap:
                    break
                entries.append({"name": _rel(ws, m), "dir": m.is_dir()})
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "path": _rel(ws, base) if base != ws else ".",
        "count": len(entries),
        "truncated": len(entries) >= cap,
        "entries": entries,
    }


_FILE_EXEC = {
    "read_file": _exec_read_file,
    "write_file": _exec_write_file,
    "edit_file": _exec_edit_file,
    "list_files": _exec_list_files,
}

# --------------------------------------------------------- dispatch

async def _execute_mcp(name: str, args: dict, session: dict) -> dict:
    """Execute a tool of an enabled MCP server (`mcp__<id>__<tool>`).

    Enabling an MCP server in the global config is the user's explicit
    consent to let the model use that server's tools. In readonly mode
    all MCP tools are denied (no side effects).
    """
    from . import mcp

    settings = session.get("settings", {})
    mode = settings.get("mode") or "write"
    if mode == "readonly":
        return {"decision": "deny", "ok": False, "error": "readonly mode: MCP tools are disabled"}
    try:
        res = await mcp.call_exposed_tool(name, args, session)
    except mcp.MCPError as e:
        return {"decision": "error", "ok": False, "error": f"MCP: {e}"}
    return {"decision": "allow", "ok": res["ok"], "output": res["output"]}


async def _execute_extend_harness(args: dict, session: dict) -> dict:
    """Meta tool of extend mode: register a new custom tool in the harness."""
    from . import extensions

    settings = session.get("settings", {})
    mode = settings.get("mode") or "write"
    if mode != "extend":
        return {"decision": "deny", "ok": False, "error": "extend_harness is only available in extend mode"}
    chat_id = session.get("id")
    try:
        spec = extensions.add_tool(args, source=f"model:{chat_id or 'unknown'}")
    except extensions.ExtensionError as e:
        return {"ok": False, "error": f"extend_harness: {e}"}
    dec.record(
        chat_id,
        "harness_extended",
        {
            "name": spec["name"],
            "kind": spec.get("kind"),
            "template": spec.get("template") or spec.get("url") or "",
            "params": spec["params"],
            "source": spec["source"],
        },
        per_chat_path=ch.decisions_path(chat_id) if chat_id else None,
    )
    return {"decision": "allow", "ok": True, "output": f"registered custom tool {spec['name']} (id {spec['id']})"}


async def _execute_custom(name: str, args: dict, session: dict) -> dict:
    """Execute a registered custom tool (command or http kind)."""
    from . import extensions

    spec = extensions.get_by_name(name)
    if spec is None:
        return {"ok": False, "error": f"unknown custom tool: {name}"}
    if not spec.get("enabled", True):
        return {"decision": "deny", "ok": False, "error": f"custom tool {name!r} is disabled"}
    chat_id = session.get("id")
    result = await extensions.execute(spec, args, session)
    dec.record(
        chat_id,
        "custom_tool_call",
        {
            "name": name,
            "kind": spec.get("kind"),
            "args": {str(k): str(v)[:200] for k, v in (args or {}).items()},
            "decision": result.get("decision"),
            "ok": result.get("ok"),
        },
        per_chat_path=ch.decisions_path(chat_id) if chat_id else None,
    )
    return result


async def _run(cmd: str, workspace: str) -> dict:
    """Run the command in a thread (subprocess blocks the event loop)."""
    try:
        return await asyncio.to_thread(sandbox.run_command, cmd, workspace)
    except Exception as e:  # noqa: BLE001 - report, don't crash the turn
        return {"ok": False, "exit": -1, "stdout": "", "stderr": str(e)}


def _execute_builtin(name: str, args: dict, session: dict, approved: bool = False) -> dict:
    """Built-in file/todo tools: confine to the workspace, approve outside paths.

    `approved=True` means the user already said yes to this exact call; the
    path may then sit outside the workspace and the op runs as-is.
    """
    chat_id = session.get("id")
    settings = session.get("settings", {})
    mode = settings.get("mode") or "write"
    workspace = session.get("workspace") or ""
    if not workspace:
        return {"ok": False, "error": "session has no workspace"}

    if name in TODO_TOOLS:
        if mode == "readonly":
            return {"decision": "deny", "ok": False, "error": "readonly mode: todo tools are disabled"}
        res = _exec_todo_add(args, session) if name == "todo_add" else _exec_todo_mark(args, session)
        res = dict(res)
        res["decision"] = "allow"
        return res

    # file tools
    if mode == "readonly" and name in ("write_file", "edit_file"):
        return {"decision": "deny", "ok": False, "error": f"readonly mode: {name} is not available"}

    p, err = _ws_path(args.get("path"), workspace, allow_outside=approved)
    if err:
        if "outside" in err and not approved:
            # outside the workspace: needs the user's explicit yes
            try:
                resolved = Path(str(args.get("path") or "")).expanduser().resolve()
            except (OSError, RuntimeError):
                resolved = None
            return {
                "decision": "approval",
                "ok": False,
                "approval": {
                    "tool": name,
                    "args": args,
                    "reason": err,
                    "paths_outside": [str(resolved)] if resolved else [],
                    "mode": mode,
                },
            }
        return {"decision": "deny", "ok": False, "error": err}

    res = _FILE_EXEC[name](args, workspace, allow_outside=approved)
    res = dict(res)
    res["decision"] = "allow"
    dec.record(
        chat_id,
        "file_tool",
        {"tool": name, "path": res.get("path", args.get("path")), "ok": res.get("ok")},
        per_chat_path=ch.decisions_path(chat_id) if chat_id else None,
    )
    return res


async def execute_tool(call: dict, session: dict, approved: bool = False) -> dict:
    """Decide and (if allowed) execute one model-requested tool call.

    `call` = {"id", "function": {"name", "arguments"}}
    Returns a JSON-serializable result dict:
      allow    -> {"decision": "allow", "ok", ...}
      deny     -> {"decision": "deny", "ok": False, "error": reason}
      approval -> {"decision": "approval", "ok": False,
                   "approval": {"tool", "args", "reason", "paths_outside", "mode"}}
    `approved=True` re-runs a call the user explicitly approved.
    """
    name = call.get("function", {}).get("name", "")
    raw_args = call.get("function", {}).get("arguments")
    try:
        args = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError:
        return {"ok": False, "error": "bad tool arguments (not JSON)"}
    if name == "extend_harness":
        return await _execute_extend_harness(args, session)
    if name.startswith("mcp__"):
        return await _execute_mcp(name, args, session)
    from . import extensions

    if extensions.get_by_name(name) is not None:
        return await _execute_custom(name, args, session)
    if name in FILE_TOOLS or name in TODO_TOOLS:
        return _execute_builtin(name, args, session, approved=approved)
    if name != "run_command":
        return {"ok": False, "error": f"unknown tool: {name}"}
    cmd = (args.get("cmd") or "").strip()
    reason = (args.get("reason") or "").strip()
    if not cmd:
        return {"ok": False, "error": "tool call has no cmd"}

    settings = session.get("settings", {})
    mode = settings.get("mode") or "write"
    workspace = session.get("workspace") or ""
    if not workspace:
        return {"ok": False, "error": "session has no workspace"}
    whitelist = cfg.load().get("allowed_outside_commands", [])

    try:
        decision = sandbox.decide(mode, cmd, workspace, whitelist)
    except sandbox.SandboxError as e:
        return {"ok": False, "error": f"sandbox: {e}"}

    if decision["action"] == "deny":
        return {
            "decision": "deny",
            "ok": False,
            "error": decision.get("reason", "denied by sandbox"),
        }
    if decision["action"] == "approval" and not approved:
        return {
            "decision": "approval",
            "ok": False,
            "approval": {
                "tool": "run_command",
                "args": {"cmd": cmd, "reason": reason},
                "cmd": cmd,
                "reason": reason,
                "paths_outside": decision.get("paths_outside", []),
                "mode": mode,
            },
        }
    result = await _run(cmd, workspace)
    return {
        "decision": "allow" if not approved else "approved",
        "ok": result["ok"],
        "exit": result["exit"],
        "stdout": result["stdout"],
        "stderr": result["stderr"],
    }


async def run_approved(call: dict, session: dict) -> dict:
    """Re-run a tool call the user explicitly approved."""
    return await execute_tool(call, session, approved=True)
