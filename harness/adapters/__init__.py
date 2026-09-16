"""Adapter registry.

Each server protocol gets an adapter class; new protocols plug in here
without touching the rest of the harness.

Adapter instances are cached per server id so a server's httpx client (and
its TCP/TLS connection) is reused across turns instead of being re-created
on every request. When a server's config changes or the server is removed,
call `drop_adapter(server_id)` to invalidate the cached instance.
"""
import asyncio
import json

from .base import AdapterError, ServerAdapter
from .openai import OpenAICompatAdapter

REGISTRY = {
    "openai": OpenAICompatAdapter,  # llama.cpp llama-server, vLLM, LM Studio, TabbyAPI, ...
}

_instances: dict[str, tuple[str, ServerAdapter]] = {}  # key -> (fingerprint, adapter)


def _fingerprint(cfg: dict) -> str:
    return json.dumps(
        [cfg.get("adapter"), cfg.get("url"), cfg.get("api_key")],
        sort_keys=True,
    )


def _safe_close(adapter: ServerAdapter) -> None:
    """Best-effort async close without an await context."""
    try:
        asyncio.ensure_future(adapter.close())
    except RuntimeError:
        pass  # no running loop; the client dies with the process


def make_adapter(cfg: dict, server_id: str | None = None) -> ServerAdapter:
    kind = cfg.get("adapter") or "openai"
    cls = REGISTRY.get(kind)
    if cls is None:
        raise AdapterError(f"unknown adapter kind: {kind!r} (known: {sorted(REGISTRY)})")
    key = server_id or "anon"
    fp = _fingerprint(cfg)
    hit = _instances.get(key)
    if hit is not None and hit[0] == fp:
        return hit[1]
    if hit is not None:
        _safe_close(hit[1])
    adapter = cls(cfg)
    _instances[key] = (fp, adapter)
    return adapter


def drop_adapter(server_id: str) -> None:
    """Drop the cached adapter for a server (config changed / server removed)."""
    hit = _instances.pop(server_id, None)
    if hit is not None:
        _safe_close(hit[1])
