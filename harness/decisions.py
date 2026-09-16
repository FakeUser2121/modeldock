"""Decision ledgers.

Every decision the user makes (model picked, params changed, mode changed,
MCP toggled, command approved/rejected, fork, compaction, ...) is recorded
as a JSONL entry in TWO places, separately:

- global ledger:  data/decisions-global.jsonl  (everything, all chats)
- per-chat ledger: <workspace>/.modeldock/<chat_id>/decisions.jsonl  (inside
  the folder the chat was started in; legacy chats: data/sessions/<chat_id>/)

Entries: {id, ts, chat_id, kind, payload}
"""
import json
import time
import uuid
from collections import deque
from pathlib import Path

from .config import GLOBAL_LEDGER, SESSIONS_DIR


def _append(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def record(chat_id: str | None, kind: str, payload: dict | None = None, per_chat_path: Path | None = None) -> dict:
    """Write one decision to the global ledger and, if a chat is involved,
    to that chat's local ledger too (inside the folder the chat lives in)."""
    entry = {
        "id": uuid.uuid4().hex[:12],
        "ts": int(time.time()),
        "chat_id": chat_id,
        "kind": kind,
        "payload": payload or {},
    }
    _append(GLOBAL_LEDGER, entry)
    if chat_id:
        target = per_chat_path or (SESSIONS_DIR / chat_id / "decisions.jsonl")
        _append(target, entry)
    return entry


def _read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def list_decisions(
    chat_id: str | None = None,
    kind: str | None = None,
    limit: int = 200,
    offset: int = 0,
    per_chat_path: Path | None = None,
) -> dict:
    """chat_id=None -> global ledger (all chats). chat_id set -> that chat's local ledger.

    Streams the file forward and keeps only the most recent `limit+offset`
    matching entries (a bounded deque): memory is O(limit), not O(file),
    so the UI stays fast even as the ledger grows.
    """
    if chat_id:
        path = per_chat_path or (SESSIONS_DIR / chat_id / "decisions.jsonl")
    else:
        path = GLOBAL_LEDGER
    keep = max(limit + offset, 0)
    window: deque = deque(maxlen=keep)
    total = 0
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if kind and e.get("kind") != kind:
                    continue
                total += 1
                window.append(e)
    entries = list(reversed(window))[offset : offset + limit]  # newest first
    return {"total": total, "entries": entries}


def kinds() -> list:
    """Distinct kinds seen in the global ledger (for UI filters)."""
    seen = set()
    for e in _read_jsonl(GLOBAL_LEDGER):
        seen.add(e.get("kind", "?"))
    return sorted(seen)
