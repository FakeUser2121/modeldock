"""One chat turn: user message -> streamed model reply -> persisted.

Yields SSE events (JSON per line, framed as `data: {...}\\n\\n` by the
endpoint):
    {"type": "start", "turn_id": str}
    {"type": "delta", "content": str}
    {"type": "tool_request", "call": {...}}          # model asked for a tool
    {"type": "approval_request", "approval_id": str, "request": {...}}
                                                     # sandbox wants the user's yes
    {"type": "tool_result", "call_id": str, "result": {...}}
    {"type": "done", "assistant": str, "usage": {...} | None}
    {"type": "error", "message": str}
    {"type": "todo", "todos": [...]}                 # todo list changed mid-turn
"""
import asyncio
import json
import time
import uuid

from . import approvals
from . import chat as ch
from . import compaction as comp
from . import config as cfg
from . import decisions as dec
from . import jj
from . import servers as srv
from . import thinking as th
from .adapters import AdapterError, make_adapter
from .tools import (
    FALLBACK_MARKER,
    MAX_TOOL_ROUNDS,
    TOOLS,
    execute_tool,
    fallback_system_note,
    load_todos,
    parse_fallback_line,
    render_todo_block,
    run_approved,
)


def _system_prompt(session: dict, mode: str, extra_tool_names: list | None = None) -> str:
    defaults = cfg.load()
    agent_id = session.get("settings", {}).get("agent_type") or "general"
    agent_prompt = ""
    for a in defaults.get("agent_types", []):
        if a.get("id") == agent_id:
            agent_prompt = a.get("prompt") or ""
            break
    extra = (session.get("settings", {}).get("system_prompt") or "").strip()
    parts = [p for p in (agent_prompt, extra) if p]
    if mode == "extend":
        from . import extensions

        names = [
            (t.get("name", ""), t.get("description") or "")
            for t in extensions.list_tools()
            if t.get("enabled")
        ]
        note = (
            "EXTEND MODE: you can extend the harness itself. When the user asks for a "
            "new capability (a new command, a new API call, a new tool), register it with "
            "the extend_harness tool: a lowercase name, a one-sentence description, a kind "
            "(command or http), a template with {param} placeholders, and the parameter list. "
            "It then becomes available to you and to later chats; the user can disable or "
            "remove any tool from the UI. Registered custom tools: "
            + (", ".join(f"{n} — {d}" for n, d in names if n) if names else "none yet")
            + ". Prefer registered custom tools over raw commands when they fit."
        )
        parts.append(note)
    if mode != "readonly":
        parts.append(fallback_system_note(extra_tool_names))
    return "\n\n".join(parts)


def _drain_fallback(buf: str) -> tuple[str, str, list]:
    """Flush complete lines of `buf` that are not fallback marker lines,
    extract marker lines as {"cmd", "reason"} objects, and hold back a
    trailing incomplete line that may be the start of a streaming marker.

    Returns (flushed_text, remaining_buf, calls).
    """
    flushed = ""
    calls = []
    while buf:
        nl = buf.find("\n")
        if nl == -1:
            line = buf
            buf = ""
            if line.strip().startswith(FALLBACK_MARKER):
                return flushed, line, calls  # marker may still be streaming
            flushed += line
            break
        line, buf = buf[:nl], buf[nl + 1 :]
        obj = parse_fallback_line(line)
        if obj is not None:
            calls.append(obj)
        else:
            flushed += line + "\n"
    return flushed, buf, calls


def _finish_fallback(buf: str) -> tuple[str, list]:
    """Handle the leftover buffer at stream end: a complete marker line
    becomes a tool call; anything else is final text."""
    if not buf:
        return "", []
    obj = parse_fallback_line(buf)
    if obj is not None and "\n" not in buf:
        return "", [obj]
    return buf, []


async def run_turn(chat_id: str, user_text: str, images: list | None = None, replay: bool = False):
    """Execute one turn and yield SSE event dicts. Persists history and
    records the turn in both ledgers on success.

    `images` is a list of data URLs (e.g. data:image/png;base64,...); when
    present the user message is sent as OpenAI content parts.

    With `replay=True` no new user message is appended: the trailing
    assistant entry is dropped and regenerated (regenerate button), or an
    orphaned trailing user entry (dropped connection) is completed instead.
    """
    meta = ch.get_session(chat_id)
    if not meta:
        yield {"type": "error", "message": "unknown session"}
        return
    settings = meta.get("settings", {})
    server_id = settings.get("server")
    model = settings.get("model")
    if not server_id or not model:
        yield {"type": "error", "message": "pick a server and model for this chat first"}
        return
    images = [
        (i or "").strip()
        for i in (images or [])
        if isinstance(i, str) and (i or "").strip().startswith("data:")
    ][:8]
    history = ch.get_history(chat_id)
    append_user = True
    if replay:
        if history and history[-1].get("role") == "assistant":
            # regenerate: drop the reply being redone
            history.pop()
            ch.save_history(chat_id, history)
        elif history and history[-1].get("role") == "user":
            # orphaned user message (e.g. dropped connection): complete it
            last = history[-1]
            content = last.get("content")
            if isinstance(content, list):
                user_text = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
            else:
                user_text = content or ""
            images = last.get("images") or images
            append_user = False
        else:
            yield {"type": "error", "message": "nothing to regenerate: this chat has no user message"}
            return
    server = srv.get_server(server_id)
    if not server:
        yield {"type": "error", "message": f"server {server_id} no longer exists"}
        return

    defaults = cfg.load().get("defaults", {})
    params = {
        "model": model,
        "temperature": settings.get("temperature", defaults.get("temperature", 0.7)),
        "top_p": settings.get("top_p", defaults.get("top_p", 1.0)),
        "max_tokens": settings.get("max_tokens", defaults.get("max_tokens", 2048)),
        "repeat_penalty": settings.get("repeat_penalty", defaults.get("repeat_penalty")),
    }
    mode = settings.get("mode") or "write"
    extra_tool_names: list[str] = []
    if mode != "readonly":
        from . import extensions
        from . import mcp

        mcp_tools = await mcp.enabled_tools()
        ext_tools = extensions.tool_schemas()
        tools = TOOLS + mcp_tools + ext_tools
        extra_tool_names = [t.get("name", "") for t in mcp_tools if t.get("name")]
        extra_tool_names += [
            (t.get("function") or {}).get("name", "")
            for t in ext_tools
            if (t.get("function") or {}).get("name")
        ]
        if mode == "extend":
            tools = tools + [extensions.EXTEND_TOOL_SCHEMA]
    else:
        tools = None

    # per-chat thinking level: this model's supported levels are probed once
    # (cached per server+model); if the requested level is unsupported the
    # next HIGHER available level is used, falling back to the highest.
    thinking_level = "off"
    thinking_adjusted = False
    try:
        level, adjusted, _caps = await th.resolve(server_id, model, settings.get("thinking"))
        thinking_level, thinking_adjusted = level, adjusted
        if thinking_level != "off":
            params["chat_template_kwargs"] = th.kwargs_for(thinking_level)
    except ValueError:
        pass  # unknown server/model or probe failed: keep going without it

    def user_message() -> dict:
        """The user turn as sent to the model: plain text, or content parts
        when images are attached."""
        if images:
            content = ([{"type": "text", "text": user_text}] if user_text else [])
            content += [{"type": "image_url", "image_url": {"url": im}} for im in images]
            return {"role": "user", "content": content}
        return {"role": "user", "content": user_text}

    def _expand(m: dict) -> dict:
        """History user entries store `images` alongside plain text; rebuild
        the content parts for the API payload."""
        imgs = m.get("images")
        if imgs and isinstance(imgs, list):
            content = ([{"type": "text", "text": m.get("content") or ""}] if m.get("content") else [])
            content += [{"type": "image_url", "image_url": {"url": im}} for im in imgs]
            return {"role": m.get("role", "user"), "content": content}
        return m

    turn_id = uuid.uuid4().hex[:8]
    messages = []
    sp = _system_prompt(meta, mode, extra_tool_names)
    if sp:
        messages.append({"role": "system", "content": sp})
    messages.extend(_expand(m) for m in history)
    if append_user:
        messages.append(user_message())

    # auto-compaction: ONLY when a context size is set for this model and
    # the estimated prompt already exceeds 85% of it
    ctx = comp.effective_context(meta, server)
    if ctx and comp.estimate_tokens(messages) >= int(ctx * comp.AUTO_THRESHOLD):
        before = len(history)
        try:
            stats = await comp.compact_history(chat_id, server, params, trigger="auto")
        except AdapterError as e:
            yield {"type": "error", "message": f"auto-compaction failed: {e}"}
            return
        if stats["compacted"]:
            history = ch.get_history(chat_id)
            messages = []
            sp = _system_prompt(meta, mode, extra_tool_names)
            if sp:
                messages.append({"role": "system", "content": sp})
            messages.extend(_expand(m) for m in history)
            if append_user:
                messages.append(user_message())
            yield {"type": "compacted", "mode": "auto", "before": before, "after": len(history)}

    yield {"type": "start", "turn_id": turn_id}

    adapter = make_adapter(server, server_id)
    assistant_full = ""
    usage = None
    total_usage: dict = {}
    tool_rounds = 0
    llm_call_idx = 0  # 0, 1, 2, ... per LLM call inside this turn
    turn_stats: list[dict] = []  # per-LLM-call tps/pps/prompt/completion
    turn_files: list[str] = []  # files this turn wrote or edited
    cached_tokens = 0
    try:
        retried_no_tools = False
        while True:
            round_messages = list(messages)
            # re-inject the todo list at the top of the prompt on the first
            # call and every 3rd LLM call of the turn, so a long autonomous
            # run cannot drift away from its plan
            if llm_call_idx % 3 == 0:
                todo_block = render_todo_block(load_todos(chat_id))
                if todo_block and round_messages and round_messages[0].get("role") == "system":
                    round_messages[0] = {
                        "role": "system",
                        "content": todo_block + "\n\n" + (round_messages[0].get("content") or ""),
                    }
            assistant_text = ""
            tool_calls = {}  # index -> openai assistant tool_call entry
            buf = ""  # text buffer so a fallback marker can span deltas
            json_calls: list[dict] = []
            got_error = None
            usage_call = None
            t_call_start = time.monotonic()
            t_first = None
            async for ev in adapter.chat_stream(round_messages, params, tools=tools):
                et = ev.get("type")
                if et == "delta":
                    if t_first is None:
                        t_first = time.monotonic()
                    buf += ev["content"]
                    flushed, buf, calls = _drain_fallback(buf)
                    for c in calls:
                        json_calls.append(c)
                    if flushed:
                        assistant_text += flushed
                        yield {"type": "delta", "content": flushed}
                elif et == "tool_calls":
                    for tc in ev["tool_calls"]:
                        i = tc.get("index", 0)
                        slot = tool_calls.setdefault(
                            i,
                            {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                        )
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["function"]["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["function"]["arguments"] += fn["arguments"]
                elif et == "usage":
                    usage_call = ev.get("usage")
                elif et == "error":
                    got_error = ev
                    break

            if got_error is not None:
                if (
                    tools
                    and not retried_no_tools
                    and got_error.get("status") in (400, 422)
                    and "tool" in (got_error.get("message") or "").lower()
                ):
                    # server rejected the tools payload: retry this round,
                    # tools-less; the JSON fallback protocol still works
                    retried_no_tools = True
                    tools = None
                    continue
                yield {"type": "error", "message": got_error.get("message", "model error")}
                return

            t_call_end = time.monotonic()
            u = usage_call or {}
            prompt_tokens = int(u.get("prompt_tokens") or 0)
            completion_tokens = int(u.get("completion_tokens") or 0)
            cached_tokens += int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
            total_usage["prompt_tokens"] = total_usage.get("prompt_tokens", 0) + prompt_tokens
            total_usage["completion_tokens"] = total_usage.get("completion_tokens", 0) + completion_tokens
            total_usage["total_tokens"] = total_usage.get("total_tokens", 0) + int(u.get("total_tokens") or 0)
            if t_first is not None:
                tps = round(completion_tokens / max(t_call_end - t_first, 1e-6), 2)
                pps = round(prompt_tokens / max(t_first - t_call_start, 1e-6), 2)
            else:
                tps = 0.0
                pps = 0.0
            turn_stats.append(
                {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "tps": tps,
                    "pps": pps,
                }
            )
            llm_call_idx += 1

            tail, tail_calls = _finish_fallback(buf)
            json_calls.extend(tail_calls)
            if tail:
                assistant_text += tail
                yield {"type": "delta", "content": tail}

            assistant_full += assistant_text

            synthetic = [
                {
                    "id": f"jsoncall_{uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {
                        "name": jc["name"],
                        "arguments": json.dumps(jc["args"]),
                    },
                }
                for jc in json_calls
                if jc.get("name")
            ]
            all_calls = [tool_calls[k] for k in sorted(tool_calls)] + synthetic
            if not all_calls:
                break

            # model asked for tools: record + execute + feed results back
            ordered = all_calls
            results = []
            for call in ordered:
                yield {"type": "tool_request", "call": call}
                result = await execute_tool(call, meta)
                if result.get("decision") == "approval":
                    req = result.get("approval", {})
                    approval_id, event = approvals.create(chat_id, call, req)
                    dec.record(
                        chat_id,
                        "approval_requested",
                        {
                            "approval_id": approval_id,
                            "cmd": req.get("cmd", ""),
                            "reason": req.get("reason", ""),
                            "paths_outside": req.get("paths_outside", []),
                        },
                        per_chat_path=ch.decisions_path(chat_id),
                    )
                    yield {"type": "approval_request", "approval_id": approval_id, "request": req}
                    try:
                        await event.wait()
                    except (GeneratorExit, asyncio.CancelledError):
                        # client went away: clean up the pending approval
                        pending = approvals.pop(approval_id)
                        if pending is not None:
                            dec.record(chat_id, "approval_cancelled", {"approval_id": approval_id}, per_chat_path=ch.decisions_path(chat_id))
                        raise
                    pending = approvals.pop(approval_id)
                    verdict = (pending or {}).get("verdict") or {}
                    dec.record(
                        chat_id,
                        "approval_decided",
                        {
                            "approval_id": approval_id,
                            "approved": bool(verdict.get("approved")),
                            "message": (verdict.get("message") or "").strip(),
                        },
                        per_chat_path=ch.decisions_path(chat_id),
                    )
                    if verdict.get("approved"):
                        result = await run_approved(call, meta)
                    else:
                        result = {
                            "ok": False,
                            "decision": "rejected",
                            "error": f"rejected by user: {verdict.get('message') or 'no reason given'}",
                        }
                fn_name = (call.get("function") or {}).get("name", "")
                if fn_name in ("write_file", "edit_file") and result.get("ok") and result.get("path"):
                    if result["path"] not in turn_files:
                        turn_files.append(result["path"])
                results.append((call, result))
                yield {"type": "tool_result", "call_id": call.get("id", ""), "result": result}
                if (
                    fn_name in ("todo_add", "todo_mark")
                    and result.get("ok")
                    and isinstance(result.get("todos"), list)
                ):
                    yield {"type": "todo", "todos": result["todos"]}
            messages.append(
                {
                    "role": "assistant",
                    "content": assistant_text or None,
                    "tool_calls": ordered,
                }
            )
            for call, result in results:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "content": json.dumps(result),
                    }
                )
            tool_rounds += 1
            if tool_rounds >= MAX_TOOL_ROUNDS:
                yield {"type": "error", "message": "stopped: too many tool rounds"}
                return
    except AdapterError as e:
        yield {"type": "error", "message": str(e)}
        return
    # adapter is cached per server id; no per-turn close

    # persist + record
    ts = time.time()
    final_usage = total_usage or usage or {}
    history = ch.get_history(chat_id)
    if append_user:
        user_entry = {"role": "user", "content": user_text, "ts": ts}
        if images:
            user_entry["images"] = images
        history.append(user_entry)
    assistant_entry = {
        "role": "assistant",
        "content": assistant_full,
        "ts": ts,
        "model": model,
        "server": server.get("name", server_id),
        "usage": final_usage,
        "turn_id": turn_id,
        "stats": turn_stats,
        "thinking": {"level": thinking_level, "adjusted": thinking_adjusted},
        "files": turn_files,
        "cached_tokens": cached_tokens,
    }
    history.append(assistant_entry)
    ch.save_history(chat_id, history)
    ch.bump_usage(chat_id, final_usage.get("total_tokens", 0))
    dec.record(
        chat_id,
        "turn",
        {
            "turn_id": turn_id,
            "model": model,
            "server": server.get("name", server_id),
            "mode": mode,
            "user": user_text,
            "assistant": assistant_full,
            "usage": final_usage,
            "thinking": thinking_level,
        },
        per_chat_path=ch.decisions_path(chat_id),
    )
    # jj auto-commit: the workspace folder is a jj repo (created on session
    # start), and this turn commits whatever the work changed. The LLM never
    # manages jj; an empty working copy commits nothing.
    summary = (assistant_full or "").strip().replace("\n", " ")
    if len(summary) > 80:
        summary = summary[:80] + "..."
    jj_message = f"chat {chat_id} turn {turn_id}: {summary or 'assistant reply'}"
    try:
        if jj.commit(meta.get("workspace", ""), jj_message):
            dec.record(
                chat_id,
                "jj_commit",
                {"turn_id": turn_id, "message": jj_message},
                per_chat_path=ch.decisions_path(chat_id),
            )
    except (RuntimeError, OSError):
        pass
    yield {
        "type": "done",
        "assistant": assistant_full,
        "usage": final_usage,
        "stats": turn_stats,
        "files": turn_files,
        "thinking": {"level": thinking_level, "adjusted": thinking_adjusted},
    }
