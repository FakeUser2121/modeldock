"""FastAPI app: serves the web GUI and the REST/SSE API.

Routes are added step by step; this module stays the single entry point.
"""
import json
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from . import config as cfg

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="modeldock", version="0.1.0")


# ---------------- health & config ----------------

@app.get("/api/health")
def health():
    return {"ok": True, "app": "modeldock", "version": "0.1.0"}


@app.get("/api/config")
def get_config():
    return cfg.load()


@app.put("/api/config")
async def put_config(request: Request):
    new = await request.json()
    cfg.save(new)
    return cfg.load()


# ---------------- chat (streaming) ----------------

@app.post("/api/sessions/{chat_id}/chat")
async def chat_endpoint(chat_id: str, request: Request):
    from . import turn as t

    body = await request.json()
    text = (body.get("message") or "").strip()
    images = body.get("images") or []
    if not isinstance(images, list):
        raise HTTPException(400, "images must be a list of data URLs")
    images = [
        (i or "").strip()
        for i in images
        if isinstance(i, str) and (i or "").strip().startswith("data:")
    ][:8]
    if not text and not images:
        raise HTTPException(400, "message is empty")

    async def event_stream():
        async for ev in t.run_turn(chat_id, text, images=images):
            yield f"data: {json.dumps(ev)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------------- chat sessions ----------------

@app.get("/api/sessions")
def list_sessions_api():
    from . import chat as ch

    return {"sessions": ch.list_sessions()}


@app.post("/api/sessions")
async def create_session_api(request: Request):
    from . import chat as ch

    body = await request.json()
    title = (body.get("title") or "").strip()
    workspace = (body.get("workspace") or "").strip()
    if not workspace:
        raise HTTPException(400, "workspace path is required")
    try:
        return ch.create_session(
            title,
            workspace,
            server=(body.get("server") or None),
            model=(body.get("model") or None),
        )
    except OSError as e:
        raise HTTPException(400, f"cannot create workspace: {e}")


@app.get("/api/sessions/{chat_id}")
def get_session_api(chat_id: str):
    from . import chat as ch

    meta = ch.get_session(chat_id)
    if not meta:
        raise HTTPException(404, "unknown session")
    return meta


@app.put("/api/sessions/{chat_id}")
async def update_session_api(chat_id: str, request: Request):
    from . import chat as ch

    body = await request.json()
    try:
        return ch.update_session(chat_id, body)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/sessions/{chat_id}/history")
def history_api(chat_id: str):
    from . import chat as ch

    if not ch.get_session(chat_id):
        raise HTTPException(404, "unknown session")
    return {"messages": ch.get_history(chat_id)}


# ---------------- compaction & fork ----------------

@app.post("/api/sessions/{chat_id}/compact")
async def compact_api(chat_id: str, request: Request):
    from . import chat as ch
    from . import compaction as comp
    from . import servers as srv

    meta = ch.get_session(chat_id)
    if not meta:
        raise HTTPException(404, "unknown session")
    settings = meta.get("settings", {})
    server_id, model = settings.get("server"), settings.get("model")
    if not server_id or not model:
        raise HTTPException(400, "pick a server and model for this chat first")
    server = srv.get_server(server_id)
    if not server:
        raise HTTPException(400, f"server {server_id} no longer exists")
    defaults = cfg.load().get("defaults", {})
    params = {
        "model": model,
        "temperature": settings.get("temperature", defaults.get("temperature", 0.7)),
        "top_p": settings.get("top_p", defaults.get("top_p", 1.0)),
        "max_tokens": settings.get("max_tokens", defaults.get("max_tokens", 2048)),
        "repeat_penalty": settings.get("repeat_penalty", defaults.get("repeat_penalty")),
    }
    from .adapters import AdapterError

    try:
        return await comp.compact_history(chat_id, server, params, trigger="manual")
    except AdapterError as e:
        raise HTTPException(502, f"compaction failed: {e}")


@app.post("/api/sessions/{chat_id}/fork")
def fork_api(chat_id: str):
    from . import chat as ch

    try:
        return ch.fork_session(chat_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/sessions/{chat_id}/context")
def context_api(chat_id: str):
    from . import compaction as comp

    try:
        return comp.context_stats(chat_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


# ---------------- approvals ----------------

@app.post("/api/sessions/{chat_id}/approve")
async def approve_api(chat_id: str, request: Request):
    from . import approvals

    body = await request.json()
    approval_id = (body.get("approval_id") or "").strip()
    if not approval_id:
        raise HTTPException(400, "approval_id is required")
    approved = bool(body.get("approved"))
    message = (body.get("message") or "").strip()
    if not approvals.resolve(approval_id, chat_id, approved, message):
        raise HTTPException(404, "no pending approval with that id for this session")
    return {"ok": True, "approved": approved}


@app.get("/api/sessions/{chat_id}/approvals")
def approvals_api(chat_id: str):
    from . import approvals
    from . import chat as ch

    if not ch.get_session(chat_id):
        raise HTTPException(404, "unknown session")
    return {"approvals": approvals.pending_for(chat_id)}


@app.delete("/api/sessions/{chat_id}")
def delete_session_api(chat_id: str):
    from . import chat as ch

    try:
        ch.delete_session(chat_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


# ---------------- servers ----------------

@app.get("/api/servers")
def list_servers():
    from . import servers as srv

    return {"servers": srv.list_servers()}


@app.post("/api/servers")
async def add_server(request: Request):
    from . import servers as srv
    from .adapters import AdapterError

    body = await request.json()
    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "url is required")
    try:
        sid = body.get("id") or uuid.uuid4().hex[:8]
        server = {
            "id": sid,
            "name": (body.get("name") or "").strip() or url,
            "url": url,
            "api_key": (body.get("api_key") or "").strip(),
            "adapter": body.get("adapter") or "openai",
            "context_size": body.get("context_size") or None,
            "model_context": body.get("model_context") or {},
            "description": (body.get("description") or "").strip(),
        }
        return srv.upsert_server(server)
    except AdapterError as e:
        raise HTTPException(400, str(e))


@app.put("/api/servers/{server_id}")
async def update_server(server_id: str, request: Request):
    from . import servers as srv
    from .adapters import AdapterError

    body = await request.json()
    cur = srv.get_server(server_id)
    if not cur:
        raise HTTPException(404, "unknown server")
    merged = {**cur, **body, "id": server_id}
    try:
        return srv.upsert_server(merged)
    except AdapterError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/servers/{server_id}")
def delete_server(server_id: str):
    from . import servers as srv

    if not srv.get_server(server_id):
        raise HTTPException(404, "unknown server")
    srv.remove_server(server_id)
    return {"ok": True}


@app.get("/api/servers/{server_id}/models")
async def server_models(server_id: str, refresh: bool = False):
    from . import servers as srv
    from .adapters import AdapterError

    try:
        return await srv.models_with_caps(server_id, refresh=refresh)
    except AdapterError as e:
        raise HTTPException(502, str(e))


@app.get("/api/servers/{server_id}/health")
async def server_health(server_id: str):
    from . import servers as srv
    from .adapters import AdapterError

    try:
        ok = await srv.server_health(server_id)
    except AdapterError as e:
        raise HTTPException(404, str(e))
    return {"server_id": server_id, "ok": ok}


# ---------------- MCP servers ----------------

@app.get("/api/mcp/servers")
async def list_mcp_servers():
    from . import mcp

    out = []
    for e in cfg.load().get("mcp_servers", []):
        tool_names = None
        try:
            tools = await mcp.list_tools_cached(e["id"])
            tool_names = [t.get("name") for t in tools if t.get("name")]
        except Exception:
            tool_names = None
        out.append({"entry": e, "tool_names": tool_names})
    return {"servers": out}


@app.post("/api/mcp/servers")
async def add_mcp_server(request: Request):
    from . import decisions as dec
    from . import mcp

    body = await request.json()
    transport = (body.get("transport") or "http").strip()
    if transport not in ("http", "stdio"):
        raise HTTPException(400, "transport must be 'http' or 'stdio'")
    url = (body.get("url") or "").strip()
    command = (body.get("command") or "").strip()
    if transport == "http" and not url:
        raise HTTPException(400, "url is required for http MCP servers")
    if transport == "stdio" and not command:
        raise HTTPException(400, "command is required for stdio MCP servers")
    servers = cfg.load().get("mcp_servers", [])
    sid = (body.get("id") or "").strip() or uuid.uuid4().hex[:8]
    if any(s.get("id") == sid for s in servers):
        raise HTTPException(400, "a MCP server with that id already exists")
    entry = {
        "id": sid,
        "name": (body.get("name") or "").strip() or (url or command),
        "transport": transport,
        "url": url,
        "command": command,
        "args": body.get("args") or [],
        "env": body.get("env") or {},
        "api_key": (body.get("api_key") or "").strip(),
        "enabled": bool(body.get("enabled", True)),
    }
    servers.append(entry)
    cfg.save({**cfg.load(), "mcp_servers": servers})
    dec.record(None, "mcp_server_added", {"id": sid, "name": entry["name"], "transport": transport})
    return entry


@app.put("/api/mcp/servers/{server_id}")
async def update_mcp_server(server_id: str, request: Request):
    from . import decisions as dec
    from . import mcp

    entry = mcp.get_entry(server_id)
    if entry is None:
        raise HTTPException(404, "unknown MCP server")
    body = await request.json()
    merged = {**entry, **body, "id": server_id}
    transport = (merged.get("transport") or "http")
    if transport not in ("http", "stdio"):
        raise HTTPException(400, "transport must be 'http' or 'stdio'")
    if transport == "http" and not (merged.get("url") or "").strip():
        raise HTTPException(400, "url is required for http MCP servers")
    if transport == "stdio" and not (merged.get("command") or "").strip():
        raise HTTPException(400, "command is required for stdio MCP servers")
    servers = [s if s.get("id") != server_id else merged for s in cfg.load().get("mcp_servers", [])]
    cfg.save({**cfg.load(), "mcp_servers": servers})
    await mcp.close_server(server_id)  # drop stale connection; reconnected lazily
    dec.record(None, "mcp_server_updated", {"id": server_id, "name": merged.get("name", server_id)})
    return merged


@app.delete("/api/mcp/servers/{server_id}")
async def delete_mcp_server(server_id: str):
    from . import decisions as dec
    from . import mcp

    entry = mcp.get_entry(server_id)
    if entry is None:
        raise HTTPException(404, "unknown MCP server")
    servers = [s for s in cfg.load().get("mcp_servers", []) if s.get("id") != server_id]
    cfg.save({**cfg.load(), "mcp_servers": servers})
    await mcp.close_server(server_id)
    dec.record(None, "mcp_server_removed", {"id": server_id, "name": entry.get("name", server_id)})
    return {"ok": True}


@app.get("/api/mcp/servers/{server_id}/tools")
async def mcp_server_tools(server_id: str, refresh: bool = False):
    from . import mcp

    try:
        tools = await mcp.list_tools_cached(server_id, refresh=refresh)
    except mcp.MCPError as e:
        raise HTTPException(502, str(e))
    return {
        "server_id": server_id,
        "tools": [
            {
                "name": t.get("name", ""),
                "description": (t.get("description") or "")[:400],
                "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
            }
            for t in tools
        ],
    }


# ---------------- custom tools (harness extensions) ----------------

@app.get("/api/tools")
def list_tools_api():
    from . import extensions

    return {"tools": extensions.list_tools()}


@app.post("/api/tools")
async def add_tool_api(request: Request):
    from . import decisions as dec
    from . import extensions

    body = await request.json()
    spec_in = dict(body)
    if (spec_in.get("kind") == "http") and not (spec_in.get("url") or "").strip():
        spec_in["url"] = spec_in.get("template") or ""
    try:
        spec = extensions.add_tool(spec_in, source="user")
    except extensions.ExtensionError as e:
        raise HTTPException(400, str(e))
    dec.record(None, "tool_added", {"id": spec["id"], "name": spec["name"], "kind": spec["kind"]})
    return spec


@app.put("/api/tools/{tool_id}")
async def update_tool_api(tool_id: str, request: Request):
    from . import decisions as dec
    from . import extensions

    spec = extensions.get_by_id(tool_id)
    if spec is None:
        raise HTTPException(404, "unknown tool")
    body = await request.json()
    merged = {**spec, **body, "id": tool_id}
    merged.pop("source", None)
    if (merged.get("kind") == "http") and not (merged.get("url") or "").strip():
        merged["url"] = merged.get("template") or ""
    try:
        norm = extensions.validate_spec(merged, ignore_id=tool_id)
    except extensions.ExtensionError as e:
        raise HTTPException(400, str(e))
    norm["source"] = spec.get("source", "user")  # creator attribution is immutable
    norm["enabled"] = bool(merged.get("enabled", True))
    extensions.replace_spec(tool_id, norm)
    dec.record(None, "tool_updated", {"id": tool_id, "name": norm["name"], "enabled": norm["enabled"]})
    return norm


@app.delete("/api/tools/{tool_id}")
async def delete_tool_api(tool_id: str):
    from . import decisions as dec
    from . import extensions

    try:
        spec = extensions.remove_tool(tool_id)
    except extensions.ExtensionError as e:
        raise HTTPException(404, str(e))
    dec.record(None, "tool_removed", {"id": tool_id, "name": spec.get("name")})
    return {"ok": True}


# ---------------- decision ledgers ----------------

@app.get("/api/decisions")
def decisions(
    chat_id: str | None = None,
    kind: str | None = None,
    limit: int = 200,
    offset: int = 0,
):
    from . import chat as ch
    from . import decisions as dec

    per_chat = ch.decisions_path(chat_id) if chat_id else None
    return dec.list_decisions(chat_id=chat_id, kind=kind, limit=limit, offset=offset, per_chat_path=per_chat)


@app.get("/api/decisions/kinds")
def decision_kinds():
    from . import decisions as dec

    return {"kinds": dec.kinds()}


# ---------------- static web assets ----------------

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/css/{name}", include_in_schema=False)
def css(name: str):
    return FileResponse(WEB_DIR / "css" / name)


@app.get("/js/{name}", include_in_schema=False)
def js(name: str):
    return FileResponse(WEB_DIR / "js" / name)


@app.get("/_sendprobe.html", include_in_schema=False)
def send_probe():
    """Functional send probe page (tests/make_send_probe.py)."""
    return FileResponse(WEB_DIR / "_sendprobe.html")


@app.get("/_probe.html", include_in_schema=False)
def layout_probe():
    """Overflow/layout probe page (tests/make_probe.py)."""
    return FileResponse(WEB_DIR / "_probe.html")
