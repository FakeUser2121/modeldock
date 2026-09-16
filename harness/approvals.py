"""Pending approvals: tool calls waiting on the user's decision.

A running turn holds its pending approval here; the user resolves it via
POST /api/sessions/{chat_id}/approve, which records the verdict and sets
the asyncio.Event the turn is awaiting on. Pending entries are in-process
state: if the turn is abandoned (client disconnect) the entry is cleaned
up and an `approval_cancelled` decision is recorded.
"""
import asyncio
import time
import uuid

PENDING: dict[str, dict] = {}


def create(chat_id: str, call: dict, request: dict) -> tuple[str, asyncio.Event]:
    approval_id = uuid.uuid4().hex[:8]
    event = asyncio.Event()
    PENDING[approval_id] = {
        "approval_id": approval_id,
        "chat_id": chat_id,
        "created_at": time.time(),
        "call": call,
        "request": request,
        "verdict": None,
        "event": event,
    }
    return approval_id, event


def pending_for(chat_id: str) -> list[dict]:
    """Pending approvals for one session (for the UI to re-render)."""
    return [
        {
            "approval_id": p["approval_id"],
            "created_at": p["created_at"],
            "call": p["call"],
            "request": p["request"],
        }
        for p in PENDING.values()
        if p["chat_id"] == chat_id
    ]


def resolve(approval_id: str, chat_id: str, approved: bool, message: str) -> bool:
    """Record the user's verdict and release the waiting turn.

    Returns False if the approval is unknown (or belongs to another chat).
    """
    p = PENDING.get(approval_id)
    if not p or p["chat_id"] != chat_id:
        return False
    p["verdict"] = {"approved": approved, "message": message}
    p["event"].set()
    return True


def pop(approval_id: str) -> dict | None:
    """Remove and return the pending entry (the turn does this after waking)."""
    return PENDING.pop(approval_id, None)
