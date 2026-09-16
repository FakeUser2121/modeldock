"""MCP (Model Context Protocol) server registry.

Every MCP server the user adds to the global config is either
- transport "http":   JSON-RPC over HTTP (streamable-HTTP transport; the
  response may be plain JSON or an SSE stream containing the JSON-RPC
  response), or
- transport "stdio":  JSON-RPC over newline-delimited JSON on a spawned
  process's stdin/stdout.

Tools of enabled MCP servers are exposed to the model as OpenAI-style
functions named `mcp__<server_id>__<tool_name>`. Enabling a server is the
user's explicit consent to let the model use its tools; in readonly mode
all MCP tools are denied (no side effects).
"""
import asyncio
import json
import os
import re
import time
from typing import Optional

from . import chat as ch
from . import config as cfg
from . import decisions as dec

TTL_SECONDS = 60


class MCPError(Exception):
    pass


def _safe(name: str, limit: int = 40) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", name)[:limit]


class MCPClient:
    """One live connection to one MCP server entry."""

    def __init__(self, entry: dict):
        self.entry = dict(entry)
        self.id = entry.get("id", "")
        self.name = (entry.get("name") or self.id or "mcp").strip()
        self.transport = (entry.get("transport") or "http").strip()
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._http = None
        self._session_id: Optional[str] = None
        self._req = 0
        self._init_done = False

    async def close(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass
            try:
                await self._proc.wait()
            except Exception:
                pass
            self._proc = None
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None
        self._session_id = None
        self._init_done = False

    # ---------------- low-level JSON-RPC ----------------

    def _next_id(self) -> int:
        self._req += 1
        return self._req

    @staticmethod
    def _unwrap(obj: dict):
        if isinstance(obj, dict) and "error" in obj:
            err = obj["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise MCPError(f"MCP error: {msg}")
        return obj.get("result") if isinstance(obj, dict) else obj

    async def _rpc_http(self, body: dict):
        if self._http is None:
            import httpx

            self._http = httpx.AsyncClient(timeout=60)
        url = (self.entry.get("url") or "").strip()
        if not url:
            raise MCPError("http MCP server has no url")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        api_key = (self.entry.get("api_key") or "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        r = await self._http.post(url, json=body, headers=headers)
        if r.status_code >= 400:
            raise MCPError(f"HTTP {r.status_code}: {(r.text or '')[:300]}")
        sid = r.headers.get("Mcp-Session-Id")
        if sid:
            self._session_id = sid
        ct = (r.headers.get("content-type") or "")
        text = r.text
        if "text/event-stream" in ct:
            for line in text.splitlines():
                if line.startswith("data:"):
                    try:
                        obj = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict) and obj.get("id") == body["id"]:
                        return self._unwrap(obj)
            raise MCPError("SSE response contained no matching JSON-RPC response")
        try:
            return self._unwrap(json.loads(text))
        except json.JSONDecodeError:
            raise MCPError(f"non-JSON response from MCP server: {text[:200]}")

    async def _rpc_stdio(self, body: dict):
        if self._proc is None:
            command = (self.entry.get("command") or "").strip()
            if not command:
                raise MCPError("stdio MCP server has no command")
            args = list(self.entry.get("args") or [])
            env = dict(os.environ)
            env.update(self.entry.get("env") or {})
            self._proc = await asyncio.create_subprocess_exec(
                command,
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        line = json.dumps(body, separators=(",", ":")) + "\n"
        self._proc.stdin.write(line.encode("utf-8"))  # write() is sync; drain() flushes
        await self._proc.stdin.drain()
        while True:
            raw = await self._proc.stdout.readline()
            if not raw:
                raise MCPError("MCP stdio server closed the connection")
            try:
                obj = json.loads(raw.decode("utf-8").strip())
            except json.JSONDecodeError:
                raise MCPError(f"non-JSON line from stdio MCP server: {raw[:200]!r}")
            if isinstance(obj, dict) and obj.get("id") == body["id"]:
                return self._unwrap(obj)

    async def _rpc(self, method: str, params: Optional[dict] = None):
        body = {"jsonrpc": "2.0", "id": self._next_id(), "method": method}
        if params is not None:
            body["params"] = params
        if self.transport == "stdio":
            return await self._rpc_stdio(body)
        return await self._rpc_http(body)

    async def _notify(self, method: str, params: Optional[dict] = None) -> None:
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if self.transport == "stdio":
            if self._proc is None:
                return
            line = json.dumps(body, separators=(",", ":")) + "\n"
            try:
                self._proc.stdin.write(line.encode("utf-8"))
                await self._proc.stdin.drain()
            except Exception:
                pass
        else:
            try:
                if self._http is None:
                    import httpx

                    self._http = httpx.AsyncClient(timeout=30)
                url = (self.entry.get("url") or "").strip()
                headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
                if self._session_id:
                    headers["Mcp-Session-Id"] = self._session_id
                await self._http.post(url, json=body, headers=headers, timeout=10)
            except Exception:
                pass  # notifications are best-effort

    # ---------------- MCP protocol ----------------

    async def initialize(self) -> None:
        if self._init_done:
            return
        await self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "modeldock", "version": "0.1.0"},
            },
        )
        await self._notify("notifications/initialized")
        self._init_done = True

    async def list_tools(self) -> list:
        await self.initialize()
        result = await self._rpc("tools/list", {})
        return (result or {}).get("tools", []) if isinstance(result, dict) else []

    async def call_tool(self, name: str, args: dict) -> dict:
        await self.initialize()
        result = await self._rpc("tools/call", {"name": name, "arguments": args or {}})
        return result if isinstance(result, dict) else {}


def mcp_result_to_text(result: dict) -> str:
    """Flatten an MCP tools/call result into text for the model."""
    parts = []
    for c in result.get("content") or []:
        if isinstance(c, dict) and c.get("type") == "text":
            parts.append(c.get("text") or "")
        elif isinstance(c, str):
            parts.append(c)
    text = "\n".join(p for p in parts if p)
    if result.get("isError") and not text:
        text = "MCP tool reported an error"
    return text or json.dumps(result)


# ---------------- registry over the global config ----------------

_clients: dict[str, MCPClient] = {}
_tool_cache: dict[str, tuple[float, list]] = {}


def all_entries() -> list:
    return cfg.load().get("mcp_servers", []) or []


def enabled_entries() -> list:
    return [e for e in all_entries() if e.get("enabled")]


def get_entry(server_id: str) -> dict | None:
    for e in all_entries():
        if e.get("id") == server_id:
            return e
    return None


async def _get_client(entry: dict) -> MCPClient:
    cur = _clients.get(entry["id"])
    if cur is not None and cur.entry == entry:
        return cur
    if cur is not None:
        await cur.close()
    client = MCPClient(entry)
    _clients[entry["id"]] = client
    return client


async def close_server(server_id: str) -> None:
    client = _clients.pop(server_id, None)
    _tool_cache.pop(server_id, None)
    if client is not None:
        await client.close()


async def list_tools_cached(server_id: str, refresh: bool = False) -> list:
    entry = get_entry(server_id)
    if entry is None:
        raise MCPError("unknown MCP server")
    now = time.time()
    cached = _tool_cache.get(server_id)
    if cached and not refresh and now - cached[0] < TTL_SECONDS:
        return cached[1]
    client = await _get_client(entry)
    tools = await client.list_tools()
    _tool_cache[server_id] = (now, tools)
    return tools


async def enabled_tools() -> list:
    """OpenAI-style tool schemas for every enabled MCP server.

    Tool lists are cached per server for TTL_SECONDS so a chat turn does
    not re-spawn stdio MCP processes (or re-POST http servers) every turn.
    A failing server is skipped silently — one dead MCP server must not
    break the chat turn.
    """
    out = []
    for entry in enabled_entries():
        try:
            tools = await list_tools_cached(entry["id"])
        except Exception:
            continue
        for t in tools:
            name = (t.get("name") or "").strip()
            if not name:
                continue
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": f"mcp__{entry['id']}__{_safe(name)}",
                        "description": (t.get("description") or f"MCP tool {name} on {entry.get('name', entry['id'])}")[:400],
                        "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
                    },
                }
            )
    return out


async def call_exposed_tool(exposed_name: str, args: dict, session: dict) -> dict:
    """Route a model tool call named `mcp__<id>__<tool>` to its MCP server."""
    m = re.match(r"^mcp__([a-z0-9]+)__(.+)$", exposed_name)
    if not m:
        raise MCPError(f"bad exposed MCP tool name: {exposed_name}")
    server_id, tool_name = m.group(1), m.group(2)
    entry = get_entry(server_id)
    if entry is None:
        raise MCPError(f"unknown MCP server {server_id}")
    client = await _get_client(entry)
    result = await client.call_tool(tool_name, args or {})
    text = mcp_result_to_text(result)
    ok = not bool(result.get("isError"))
    dec.record(
        session.get("id"),
        "mcp_tool_call",
        {
            "server_id": server_id,
            "server": entry.get("name", server_id),
            "tool": tool_name,
            "args": args or {},
            "ok": ok,
            "output": text[:1000],
        },
        per_chat_path=ch.decisions_path(session.get("id")) if session.get("id") else None,
    )
    return {"ok": ok, "output": text}
