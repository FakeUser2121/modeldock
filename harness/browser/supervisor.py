"""Per-chat mdock-cdp sidecar supervisor (Stage 2 glue).

One persistent `mdock-cdp --serve` process per chat, lazily started on
first use, torn down after idle_timeout_s of inactivity or on explicit
close. The sidecar speaks JSONL over stdin/stdout (one command in, one
response out) and owns the Chromium process itself (Stage 1, Go layer:
headful window, software-GL/SwiftShader CPU-only rendering, never GPU).

Per-chat state lives in the chat's session folder:

    <session_dir>/browser/{profile/, frame.jpg, state.json, sidecar.log}

Protocol (Stage 1 sidecar):
    in:  {"id":"...","cmd":"open|navigate|eval|screenshot|screencast_start
                         |screencast_stop|status|close", ...}
    out: {"id":"...","ok":true,"value":...}
         {"id":"...","ok":false,"error":"..."}

Resource discipline: no browser process exists until the first browser
action of the chat; idle browsers are torn down automatically, so RAM/
CPU usage stays flat when the browser is not being used.
"""
import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

from .. import chat as ch
from .. import config as cfg

# The Go sidecar binary. Override with MODELDOCK_CDP_BIN if built elsewhere.
BIN = Path(
    os.environ.get("MODELDOCK_CDP_BIN")
    or Path(__file__).resolve().parent.parent.parent / "go" / "cdpgate" / "mdock-cdp"
)

CMD_TIMEOUT_S = 90.0   # `open` spawns chromium (~10-20s) then attaches
STOP_TIMEOUT_S = 20.0
IDLE_POLL_S = 5.0


class BrowserError(Exception):
    """Supervisor-level failure (timeout, bad response, unknown session)."""


class BrowserNotRunning(BrowserError):
    """No sidecar process for this chat (never started or already torn down)."""


_registry: dict = {}
_registry_lock = threading.Lock()
_idle_thread: threading.Thread | None = None


class _Browser:
    """One sidecar process + its per-chat files."""

    def __init__(self, chat_id: str, session_dir: Path):
        self.chat_id = chat_id
        self.dir = Path(session_dir) / "browser"
        self.profile = self.dir / "profile"
        self.frame_file = self.dir / "frame.jpg"
        self.state_file = self.dir / "state.json"
        self.proc: subprocess.Popen | None = None
        self.reader: queue.Queue | None = None
        self.lock = threading.Lock()
        self.started_at = 0.0
        self.last_activity = 0.0
        self.url = ""
        self.screencast = False

    # ---------------------------------------------------------- lifecycle

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def ensure_started(self, url: str = "") -> dict:
        """Spawn the sidecar (and Chromium) on first use; idempotent.

        Note: status() re-acquires self.lock, so it must be called AFTER
        releasing it (threading.Lock is not reentrant).
        """
        with self.lock:
            if not self.is_running():
                self._spawn(url or "about:blank")
        return self.status()

    def _spawn(self, url: str) -> None:
        self.profile.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        home = os.environ.get("HOME") or os.path.expanduser("~")
        # Chromium needs a writable HOME; in sandboxed test invocations the
        # real $HOME may not be, so fall back to a profile-local one.
        if not (Path(home).is_dir() and os.access(home, os.W_OK)):
            home = str(self.profile / "home")
            Path(home).mkdir(parents=True, exist_ok=True)
        env["HOME"] = home
        log = open(self.profile / "sidecar.log", "ab")
        self.proc = subprocess.Popen(
            [str(BIN), "--serve", str(self.profile)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=log,
            env=env,
            cwd=str(self.profile),
        )
        self.reader = queue.Queue()
        threading.Thread(target=self._reader_loop, daemon=True).start()
        self.started_at = time.time()
        self.last_activity = time.time()
        res = self._send({"cmd": "open", "url": url}, timeout=CMD_TIMEOUT_S)
        if not res.get("ok"):
            raise BrowserError(f"sidecar open failed: {res.get('error')}")
        v = res.get("value") or {}
        self.url = v.get("url") or url
        self._save_state()

    def _reader_loop(self) -> None:
        assert self.proc is not None
        try:
            for line in self.proc.stdout:
                self.reader.put(line.decode("utf-8", "replace"))
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------ commands

    def send(self, cmd: str, **fields) -> dict:
        """Send one JSONL command and return the parsed response dict."""
        with self.lock:
            if not self.is_running():
                raise BrowserNotRunning(f"browser not running for chat {self.chat_id}")
            if cmd == "screencast_start":
                fields.setdefault("path", str(self.frame_file))
            res = self._send({"cmd": cmd, **fields})
            if res.get("ok"):
                if cmd == "screencast_start":
                    self.screencast = True
                elif cmd in ("screencast_stop", "close"):
                    self.screencast = False
                if cmd == "close":
                    self._await_exit()
            return res

    def _send(self, cmd: dict, timeout: float = CMD_TIMEOUT_S) -> dict:
        line = dict(cmd)
        line["id"] = str(int(time.monotonic() * 1000))
        payload = (json.dumps(line) + "\n").encode("utf-8")
        self.proc.stdin.write(payload)
        self.proc.stdin.flush()
        self.last_activity = time.time()
        try:
            raw = self.reader.get(timeout=timeout)
        except queue.Empty:
            raise BrowserError(f"sidecar timed out after {timeout:.0f}s (cmd={cmd.get('cmd')!r})")
        try:
            res = json.loads(raw)
        except json.JSONDecodeError:
            raise BrowserError(f"bad sidecar response: {raw[:200]!r}")
        self._save_state()
        return res

    def _await_exit(self) -> None:
        try:
            self.proc.wait(timeout=STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None
        self.reader = None
        self.url = ""

    # -------------------------------------------------------------- state

    def status(self) -> dict:
        """Live status dict for the UI routes and the browser_* tools."""
        with self.lock:
            base = {
                "running": self.is_running(),
                "chat_id": self.chat_id,
                "url": self.url or None,
                "screencast": self.screencast,
                "started_at": self.started_at or None,
                "last_activity": self.last_activity or None,
                "frame_file": str(self.frame_file),
                "profile": str(self.profile),
            }
            if not base["running"]:
                return base
            res = self._send({"cmd": "status"}, timeout=15.0)
            v = res.get("value") or {}
            base["pid"] = self.proc.pid
            base["url"] = v.get("url") or self.url
            try:
                st = self.frame_file.stat()
                base["frame"] = {"mtime": st.st_mtime, "size": st.st_size}
            except FileNotFoundError:
                base["frame"] = None
            return base

    def frame_bytes(self) -> bytes | None:
        try:
            return self.frame_file.read_bytes()
        except FileNotFoundError:
            return None

    def close(self) -> dict:
        with self.lock:
            was_running = self.is_running()
            if was_running:
                try:
                    self._send({"cmd": "close"}, timeout=STOP_TIMEOUT_S)
                except (BrowserError, OSError):
                    pass
                self._await_exit()
            self.screencast = False
            self._save_state()
            return {"closed": True, "was_running": was_running}

    def _save_state(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(
                json.dumps(
                    {
                        "chat_id": self.chat_id,
                        "running": self.is_running(),
                        "pid": self.proc.pid if self.proc is not None else None,
                        "started_at": self.started_at or None,
                        "last_activity": self.last_activity or None,
                        "url": self.url or None,
                        "screencast": self.screencast,
                        "frame_file": str(self.frame_file),
                        "profile": str(self.profile),
                    },
                    indent=1,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass


# ------------------------------------------------------------ module API

def _get(chat_id: str, create: bool = False) -> _Browser | None:
    meta = ch.get_session(chat_id)
    if meta is None:
        return None
    with _registry_lock:
        b = _registry.get(chat_id)
        if b is None and create:
            b = _Browser(chat_id, ch.session_dir(chat_id))
            _registry[chat_id] = b
        return b


def _idle_watch() -> None:
    global _idle_thread
    if _idle_thread is None or not _idle_thread.is_alive():
        _idle_thread = threading.Thread(target=_idle_loop, daemon=True)
        _idle_thread.start()


def _idle_loop() -> None:
    """Tear down browsers idle longer than browser.idle_timeout_s."""
    while True:
        time.sleep(IDLE_POLL_S)
        try:
            bcfg = cfg.load().get("browser") or {}
            timeout = float(bcfg.get("idle_timeout_s", 300))
        except Exception:
            timeout = 300.0
        now = time.time()
        with _registry_lock:
            items = list(_registry.values())
        for b in items:
            if b.is_running() and b.last_activity and now - b.last_activity > timeout:
                try:
                    b.close()
                except Exception:
                    pass


def ensure_started(chat_id: str, url: str = "") -> dict:
    """Lazily spawn the per-chat sidecar; returns a live status dict."""
    b = _get(chat_id, create=True)
    if b is None:
        raise BrowserError(f"unknown session {chat_id!r}")
    _idle_watch()
    return b.ensure_started(url)


def send(chat_id: str, cmd: str, **fields) -> dict:
    """Send one sidecar command for this chat; raises BrowserNotRunning."""
    b = _get(chat_id)
    if b is None:
        raise BrowserError(f"unknown session {chat_id!r}")
    return b.send(cmd, **fields)


def status(chat_id: str) -> dict:
    b = _get(chat_id)
    if b is None:
        return {"running": False, "chat_id": chat_id}
    return b.status()


def frame_bytes(chat_id: str) -> bytes | None:
    b = _get(chat_id)
    if b is None:
        return None
    return b.frame_bytes()


def close(chat_id: str) -> dict:
    b = _get(chat_id)
    if b is None:
        return {"closed": True, "was_running": False}
    return b.close()


def shutdown_all() -> None:
    """Tear down every sidecar (server shutdown hook)."""
    with _registry_lock:
        items = list(_registry.values())
    for b in items:
        try:
            b.close()
        except Exception:
            pass
