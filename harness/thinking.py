"""Per-chat reasoning effort ("thinking level").

Models that accept a thinking level get llama.cpp `chat_template_kwargs`
(`reasoning_effort` / `enable_thinking` / `preserve_thinking`) injected on
every request. Which levels a model actually accepts is unknown a priori,
so this module PROBE-TESTS the real server: for each level from low to high,
it sends one minimal non-streaming request and keeps the first level that
answers without a template error.

Fallback rule (per the user): if the requested level is missing from the
model's accepted list, use the next HIGHER available level; if none is
higher, use the highest available; if none at all, run with thinking off.

Probe results are cached per (server, model) in the server's `model_caps`
dict (next to the vision caps) with a TTL.
"""
import time

from . import servers as srv
from .adapters import make_adapter

# ordered from least to most reasoning
THINKING_LEVELS = ("off", "low", "medium", "high", "xhigh")
LEVEL_RANK = {lv: i for i, lv in enumerate(THINKING_LEVELS)}

PROBE_TIMEOUT = 60.0
PROBE_MAX_TOKENS = 64
CAPS_TTL = 6 * 3600.0  # re-probe after 6 hours


def kwargs_for(level: str) -> dict | None:
    """chat_template_kwargs for a thinking level (None = no kwargs at all)."""
    level = (level or "off").lower()
    if level not in LEVEL_RANK or level == "off":
        return None
    return {
        "reasoning_effort": level,
        "enable_thinking": True,
        "preserve_thinking": True,
    }


async def probe_model(server_id: str, model: str) -> dict:
    """Determine which thinking levels `model` on `server_id` accepts.

    Returns {"server", "model", "supported": [levels...], "probed_at", "errors": {...}}.
    `supported` is ordered; may be empty (thinking not usable on this model).
    """
    server_cfg = srv.get_server(server_id)
    if not server_cfg:
        raise ValueError(f"unknown server: {server_id}")
    adapter = make_adapter(server_cfg, server_id)
    messages = [{"role": "user", "content": "Reply with exactly: ok"}]
    supported: list[str] = []
    errors: dict[str, str] = {}
    for level in THINKING_LEVELS:
        if level == "off":
            supported.append("off")
            continue
        ctk = kwargs_for(level)
        try:
            params = {"model": model, "max_tokens": PROBE_MAX_TOKENS}
            if ctk:
                params["chat_template_kwargs"] = ctk
            res = await adapter.complete(messages, params, timeout=PROBE_TIMEOUT)
            if res.get("ok"):
                supported.append(level)
            else:
                errors[level] = (res.get("error") or f"HTTP {res.get('status')}")[:200]
        except Exception as e:  # noqa: BLE001 - probe failure must not raise
            errors[level] = str(e)[:200]
    return {
        "server": server_id,
        "model": model,
        "supported": supported,
        "errors": errors,
        "probed_at": time.time(),
    }


def effective_level(requested: str, supported: list[str]) -> tuple[str, bool]:
    """Apply the fallback rule.

    Returns (level, adjusted) where `adjusted` is True when the model could
    not honour the requested level and something else was chosen instead.
    `supported` must contain "off".
    """
    requested = (requested or "off").lower()
    if requested in supported:
        return requested, False
    # next HIGHER available level
    rank = LEVEL_RANK.get(requested, -1)
    for lv in THINKING_LEVELS:
        if lv in supported and LEVEL_RANK[lv] > rank:
            return lv, True
    # nothing higher: highest available (may be off)
    best = max((lv for lv in supported), key=lambda lv: LEVEL_RANK[lv], default="off")
    if LEVEL_RANK[best] > rank:
        return best, True
    return best, True


async def get_caps(server_id: str, model: str, refresh: bool = False) -> dict | None:
    """Probe (or reuse a recent cached probe) for the model's thinking support.

    The result is persisted into the server's `model_caps` entry so the UI
    can show it without re-probing.
    """
    entry = srv.model_caps_get(server_id, model)
    if entry and not refresh:
        caps = entry.get("thinking")
        if caps and time.time() - float(caps.get("probed_at", 0)) < CAPS_TTL:
            return caps
    caps = await probe_model(server_id, model)
    entry = srv.model_caps_get(server_id, model) or {}
    entry["thinking"] = caps
    srv.model_caps_set(server_id, model, entry)
    return caps


async def resolve(server_id: str, model: str, requested: str | None) -> tuple[str, bool, dict | None]:
    """Resolve a requested thinking level for a model to kwargs for the API.

    Returns (effective_level, adjusted, caps_or_None).
    Raises ValueError when the server/model is unknown.
    """
    caps = await get_caps(server_id, model)
    if caps is None:
        return "off", False, None
    level, adjusted = effective_level(requested or "off", caps.get("supported") or ["off"])
    return level, adjusted, caps
