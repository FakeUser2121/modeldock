"""Server registry + model auto-discovery.

Servers are user-defined in the global config: {id, name, url, api_key,
adapter, context_size, model_context, description}. Discovery results are
cached briefly so the UI doesn't hammer the model servers.
"""
import time

from . import decisions as dec
from .adapters import AdapterError, make_adapter

TTL_SECONDS = 60.0
_cache: dict[str, tuple[float, list]] = {}


def list_servers() -> list:
    from .config import load

    return load().get("servers", [])


def get_server(server_id: str) -> dict | None:
    for s in list_servers():
        if s.get("id") == server_id:
            return s
    return None


def upsert_server(server: dict) -> dict:
    from . import config as cfg

    c = cfg.load()
    servers = c.setdefault("servers", [])
    for i, s in enumerate(servers):
        if s.get("id") == server.get("id"):
            servers[i] = server
            dec.record(None, "server_updated", {"server_id": server["id"], "name": server.get("name")})
            break
    else:
        servers.append(server)
        dec.record(
            None,
            "server_added",
            {"server_id": server["id"], "name": server.get("name"), "url": server.get("url")},
        )
    cfg.save(c)
    _cache.pop(server.get("id"), None)
    from . import adapters

    adapters.drop_adapter(server.get("id"))  # config may have changed
    return server


def model_caps_get(server_id: str, model_id: str) -> dict | None:
    """Persisted per-model capabilities on the server entry (vision, thinking, ...)."""
    server = get_server(server_id)
    if not server:
        return None
    caps = server.get("model_caps") or {}
    return caps.get(model_id)


def model_caps_set(server_id: str, model_id: str, caps: dict) -> None:
    """Persist per-model capabilities on the server entry (merges into model_caps)."""
    from . import config as cfg

    c = cfg.load()
    for s in c.get("servers", []):
        if s.get("id") == server_id:
            s.setdefault("model_caps", {})[model_id] = caps
            break
    cfg.save(c)


def remove_server(server_id: str) -> None:
    from . import config as cfg

    c = cfg.load()
    c["servers"] = [s for s in c.get("servers", []) if s.get("id") != server_id]
    cfg.save(c)
    _cache.pop(server_id, None)
    from . import adapters

    adapters.drop_adapter(server_id)
    dec.record(None, "server_removed", {"server_id": server_id})


async def discover_models(server_id: str, refresh: bool = False) -> dict:
    server = get_server(server_id)
    if not server:
        raise AdapterError("unknown server")
    now = time.time()
    hit = _cache.get(server_id)
    if hit and not refresh and now - hit[0] < TTL_SECONDS:
        return {"server_id": server_id, "models": hit[1]}
    # adapter is cached per server id (connection reuse across requests)
    adapter = make_adapter(server, server_id)
    models = await adapter.list_models()
    _cache[server_id] = (now, models)
    return {"server_id": server_id, "models": models}


async def server_health(server_id: str) -> bool:
    server = get_server(server_id)
    if not server:
        raise AdapterError("unknown server")
    adapter = make_adapter(server, server_id)
    return await adapter.health()


async def models_with_caps(server_id: str, refresh: bool = False) -> dict:
    """Discover models and attach each one's persisted vision capability.

    Vision is probed once per model (1x1 image with a trivial prompt): a
    clean completion means the model accepts image content. Results are
    persisted on the server entry as `model_caps`, so models are not
    re-probed (and the probe cost never repeats).
    """
    result = await discover_models(server_id, refresh=refresh)
    models = result.get("models", [])
    server = get_server(server_id)
    caps = dict((server or {}).get("model_caps") or {})
    model_ids = {m.get("id") for m in models if m.get("id")}
    changed = False
    if refresh:
        for mid in list(caps):
            if mid not in model_ids:
                caps.pop(mid)
                changed = True
    if models:
        adapter = make_adapter(server, server_id)
        for m in models:
            mid = m.get("id")
            if mid is None:
                continue
            if caps.get(mid, {}).get("vision") is None:
                try:
                    caps[mid] = {"vision": bool(await adapter.probe_vision(mid))}
                except Exception:
                    caps[mid] = {"vision": False}
                changed = True
    if changed:
        from . import config as cfg

        c = cfg.load()
        for s in c.get("servers", []):
            if s.get("id") == server_id:
                s["model_caps"] = caps
                break
        cfg.save(c)
        dec.record(
            None,
            "model_caps_updated",
            {"server_id": server_id, "caps": {k: v.get("vision") for k, v in caps.items()}},
        )
    out = []
    for m in models:
        mid = m.get("id")
        out.append({**m, "vision": bool((caps.get(mid) or {}).get("vision"))})
    return {"server_id": server_id, "models": out}
