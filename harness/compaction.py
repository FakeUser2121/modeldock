"""Context compaction: keep the recent messages, summarize the older ones.

The summary is produced by the chat's own model so it works with any
adapter. Compaction keeps:
    - the last KEEP_RECENT messages verbatim (starting at a user message)
    - one system message with the summary of everything before that

Auto-compaction runs at the start of a turn, but ONLY when a context size
is set for the model (per-model `model_context` override, else the
server-wide `context_size`), and only when the estimated prompt exceeds
AUTO_THRESHOLD (85%) of that context size. Manual compaction is always
available from the UI and ignores the threshold.

Every compaction is recorded in both ledgers (kind "compaction").
"""
import time

from . import chat as ch
from . import decisions as dec
from .adapters import REGISTRY, AdapterError

KEEP_RECENT = 8
AUTO_THRESHOLD = 0.85

SUMMARY_SYSTEM = (
    "You are a context-compaction assistant. You are given the earlier part of a "
    "conversation between a user and an AI agent. Write a faithful, compact summary "
    "that preserves: decisions made, important facts and figures, file paths and "
    "workspace locations, commands that were run and their results, what was written "
    "or changed, open questions, and any standing instructions. Plain text, no "
    "preamble, under 300 words."
)


def estimate_tokens(messages: list) -> int:
    """Rough token estimate (~1 token per 4 characters, small per-message overhead).

    Good enough to decide the 85% auto-compact threshold; the adapter's real
    usage numbers remain the source of truth for what the model actually saw.
    """
    n = 0
    for m in messages:
        n += 4
        c = m.get("content")
        if isinstance(c, str):
            n += len(c) // 4
        elif isinstance(c, list):
            # OpenAI content parts (text + image_url); count the text and a
            # flat allowance per image
            for part in c:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        n += len(part.get("text") or "") // 4
                    elif part.get("type") == "image_url":
                        n += 80
        for tc in m.get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            n += (len(fn.get("name") or "") + len(fn.get("arguments") or "")) // 4
    return n


def effective_context(session: dict, server: dict) -> int | None:
    """Context window for this chat's model, or None if the user never set one."""
    model = (session.get("settings") or {}).get("model")
    mc = (server or {}).get("model_context") or {}
    if model and isinstance(mc.get(model), (int, float)) and mc.get(model) > 0:
        return int(mc[model])
    cs = (server or {}).get("context_size")
    if isinstance(cs, (int, float)) and cs > 0:
        return int(cs)
    return None


def context_stats(chat_id: str) -> dict:
    """Estimate vs configured context for the session (UI meter + checks)."""
    meta = ch.get_session(chat_id)
    if not meta:
        raise ValueError("unknown session")
    settings = meta.get("settings", {})
    server = None
    from . import servers as srv

    if settings.get("server"):
        server = srv.get_server(settings["server"])
    history = ch.get_history(chat_id)
    estimate = estimate_tokens(history)
    context = effective_context(meta, server)
    ratio = round(estimate / context, 4) if context else None
    return {
        "estimate": estimate,
        "context": context,
        "ratio": ratio,
        "threshold": AUTO_THRESHOLD,
        "would_auto_compact": bool(ratio is not None and ratio >= AUTO_THRESHOLD),
        "messages": len(history),
    }


def _summarizable(old: list) -> list:
    """Flatten older history into a plain user/assistant sequence that any
    OpenAI-compatible endpoint will accept (no dangling tool calls)."""
    out = []
    for m in old:
        role = m.get("role")
        content = m.get("content") or ""
        if isinstance(content, list):
            content = " ".join(
                (p.get("text") or "[image]") for p in content if isinstance(p, dict)
            )
        if role == "assistant":
            out.append({"role": "assistant", "content": content})
        elif role == "user":
            out.append({"role": "user", "content": content})
        elif role == "tool":
            # tool results get folded into user-flavored context lines
            out.append({"role": "user", "content": f"[tool result] {content}"})
        # system prompts are re-injected per turn; skip them here
    return out


async def chat_text(adapter, messages: list, params: dict) -> tuple[str, dict | None]:
    """Run a stream and collect the plain text (no tools)."""
    text = ""
    usage = None
    async for ev in adapter.chat_stream(messages, params, tools=None):
        t = ev.get("type")
        if t == "delta":
            text += ev.get("content", "")
        elif t == "usage":
            usage = ev.get("usage")
        elif t == "error":
            raise AdapterError(ev.get("message") or "model error")
    return text, None if usage is None else usage


async def compact_history(chat_id: str, server: dict, params: dict, trigger: str) -> dict:
    """Summarize the older part of the history in place.

    Returns {"compacted": bool, "summarized": n, "kept": n, "summary": str}.
    """
    history = ch.get_history(chat_id)
    if len(history) <= KEEP_RECENT:
        return {"compacted": False, "summarized": 0, "kept": len(history), "summary": ""}

    old, recent = history[:-KEEP_RECENT], history[-KEEP_RECENT:]
    # never start the kept window mid-exchange: walk back to a user message
    while recent and recent[0].get("role") != "user":
        old.append(recent.pop(0))
    if not old:
        return {"compacted": False, "summarized": 0, "kept": len(recent), "summary": ""}

    summarize_params = dict(params)
    summarize_params["temperature"] = 0.1
    summarize_params["max_tokens"] = min(summarize_params.get("max_tokens") or 1024, 1024)

    # One-shot, uncached adapter: compaction must never close (or poison)
    # the per-server cached client that the chat turns are reusing.
    kind = server.get("adapter") or "openai"
    cls = REGISTRY.get(kind)
    if cls is None:
        raise AdapterError(f"unknown adapter kind: {kind!r} (known: {sorted(REGISTRY)})")
    adapter = cls(server)
    try:
        text, _usage = await chat_text(
            adapter,
            [{"role": "system", "content": SUMMARY_SYSTEM}] + _summarizable(old),
            summarize_params,
        )
    finally:
        await adapter.close()

    summary = (text or "").strip() or "(no summary produced)"
    new_history = [
        {
            "role": "system",
            "content": (
                "[Compacted history]\nThe earlier part of this conversation was "
                f"summarized so it would fit the context window:\n\n{summary}"
            ),
            "ts": time.time(),
            "compacted_from": len(old),
        }
    ] + recent
    ch.save_history(chat_id, new_history)
    dec.record(
        chat_id,
        "compaction",
        {
            "trigger": trigger,
            "summarized": len(old),
            "kept": len(recent),
            "summary_words": len(summary.split()),
            "model": params.get("model"),
        },
        per_chat_path=ch.decisions_path(chat_id),
    )
    return {"compacted": True, "summarized": len(old), "kept": len(recent), "summary": summary}
