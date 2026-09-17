"""Browser subsystem (Ego-Lite-style CDP agent tools).

Stage 2: the per-chat mdock-cdp sidecar supervisor — one persistent
`mdock-cdp --serve` process per chat, lazily started, idle-teardown,
state under the chat's session folder. Later stages add the live frame
pane (3), the ref registry (4), approval gates (5) and the browser_*
tools (9). See docs/ego-lite-integration-plan.md.
"""
from .supervisor import (
    BrowserError,
    BrowserNotRunning,
    close,
    ensure_started,
    frame_bytes,
    send,
    shutdown_all,
    status,
)

__all__ = [
    "BrowserError",
    "BrowserNotRunning",
    "close",
    "ensure_started",
    "frame_bytes",
    "send",
    "shutdown_all",
    "status",
]
