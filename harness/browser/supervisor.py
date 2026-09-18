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


def _close_streams(proc: subprocess.Popen) -> None:
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass


class _Browser:
    """One sidecar process + its per-chat files."""

    def __init__(self, chat_id: str, session_dir: Path):
        self.chat_id = chat_id
        self.dir = Path(session_dir) / "browser"
        self.profile = self.dir / "profile"
        self.frame_file = self.dir / "frame.jpg"
        self.state_file = self.dir / "state.json"
        self.proc: subprocess.Popen | None = None
        self.log_file = None
        # Reentrant: ensure_started() -> status() re-enters, and so does
        # close() -> _await_exit(). A plain Lock deadlocks on those paths.
        self.lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._pending: dict[str, dict] = {}
        self._pending_lock = threading.Lock()
        self._seq = 0
        self._reader_thread: threading.Thread | None = None
        self.started_at = 0.0
        self.last_activity = 0.0
        self.url = ""
        self.screencast = False

    # ---------------------------------------------------------- lifecycle

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def ensure_started(self, url: str = "") -> dict:
        """Spawn the sidecar (and Chromium) on first use; idempotent."""
        with self.lock:
            if not self.is_running():
                self._spawn(url or "about:blank")
            return self.status()

    def _spawn(self, url: str) -> None:
        if not Path(BIN).exists():
            raise BrowserError(
                f"CDP sidecar binary not found at {BIN}. Build it with "
                "`cd go/cdpgate && go build -o mdock-cdp .`, or point "
                "MODELDOCK_CDP_BIN at it."
            )
        self.profile.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        home = os.environ.get("HOME") or os.path.expanduser("~")
        # Chromium needs a writable HOME; in sandboxed test invocations the
        # real $HOME may not be, so fall back to a profile-local one.
        if not (Path(home).is_dir() and os.access(home, os.W_OK)):
            home = str(self.profile / "home")
            Path(home).mkdir(parents=True, exist_ok=True)
        env["HOME"] = home
        self.log_file = open(self.profile / "sidecar.log", "ab")
        try:
            self.proc = subprocess.Popen(
                [str(BIN), "--serve", str(self.profile)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self.log_file,
                env=env,
                cwd=str(self.profile),
            )
        except OSError as e:
            self._close_log()
            raise BrowserError(f"cannot start sidecar {BIN}: {e}") from e
        with self._pending_lock:
            self._pending.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, args=(self.proc,), daemon=True
        )
        self._reader_thread.start()
        self.started_at = time.time()
        self.last_activity = time.time()
        try:
            res = self._send({"cmd": "open", "url": url}, timeout=CMD_TIMEOUT_S)
        except BrowserError:
            self._force_stop()
            raise
        if not res.get("ok"):
            self._force_stop()
            raise BrowserError(f"sidecar open failed: {res.get('error')}")
        v = res.get("value") or {}
        self.url = v.get("url") or url
        self._save_state()

    def _reader_loop(self, proc: subprocess.Popen) -> None:
        """Route each response line to the waiter that asked for it.

        Responses are matched on the `id` echoed by the sidecar. Without
        that matching, one timed-out command left its late reply in a plain
        FIFO and every later command got the *previous* command's answer --
        a permanent off-by-one: `status` returning a stale URL, `eval`
        returning the screenshot's result, and so on.
        """
        try:
            for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    res = json.loads(line)
                except json.JSONDecodeError:
                    continue  # sidecar noise on stdout; ignore
                rid = str(res.get("id") or "")
                with self._pending_lock:
                    slot = self._pending.pop(rid, None)
                if slot is None:
                    continue  # late reply to an abandoned command: drop it
                slot["response"] = res
                slot["event"].set()
        except (OSError, ValueError):
            pass
        finally:
            # stdout closed: the sidecar is gone. Wake everyone waiting.
            with self._pending_lock:
                slots = list(self._pending.values())
                self._pending.clear()
            for slot in slots:
                slot["response"] = None
                slot["event"].set()

    # ------------------------------------------------------------ commands

    def send(self, cmd: str, **fields) -> dict:
        """Send one JSONL command and return the parsed response dict."""
        with self.lock:
            if not self.is_running():
                raise BrowserNotRunning(f"browser not running for chat {self.chat_id}")
            if cmd == "screencast_start":
                fields.setdefault("path", str(self.frame_file))
                self.frame_file.parent.mkdir(parents=True, exist_ok=True)
            res = self._send({"cmd": cmd, **fields})
            if res.get("ok"):
                if cmd == "screencast_start":
                    self.screencast = True
                elif cmd in ("screencast_stop", "close"):
                    self.screencast = False
                if cmd == "close":
                    self._await_exit()
            return res

    def _next_id(self) -> str:
        with self._pending_lock:
            self._seq += 1
            return f"{self._seq}"

    def _send(self, cmd: dict, timeout: float | None = None) -> dict:
        timeout = CMD_TIMEOUT_S if timeout is None else timeout
        proc = self.proc
        if proc is None or proc.poll() is not None:
            raise BrowserNotRunning(f"browser not running for chat {self.chat_id}")
        line = dict(cmd)
        rid = self._next_id()
        line["id"] = rid
        slot = {"event": threading.Event(), "response": None}
        with self._pending_lock:
            self._pending[rid] = slot
        payload = (json.dumps(line) + "\n").encode("utf-8")
        try:
            with self._write_lock:
                proc.stdin.write(payload)
                proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as e:
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise BrowserError(f"sidecar stdin closed (cmd={cmd.get('cmd')!r}): {e}") from e
        self.last_activity = time.time()

        deadline = time.monotonic() + timeout
        while True:
            if slot["event"].wait(timeout=0.25):
                break
            if proc.poll() is not None:
                # process died; the reader's finally clause wakes us, but do
                # not rely on it if stdout was already closed.
                slot["event"].wait(timeout=0.5)
                break
            if time.monotonic() >= deadline:
                with self._pending_lock:
                    self._pending.pop(rid, None)
                raise BrowserError(
                    f"sidecar timed out after {timeout:.0f}s (cmd={cmd.get('cmd')!r})"
                )
        res = slot["response"]
        if res is None:
            code = proc.poll()
            raise BrowserError(
                f"sidecar exited (code={code}) while running {cmd.get('cmd')!r}; "
                f"see {self.profile / 'sidecar.log'}"
            )
        self.last_activity = time.time()
        self._save_state()
        return res

    def _close_log(self) -> None:
        try:
            if self.log_file is not None:
                self.log_file.close()
        except OSError:
            pass
        self.log_file = None

    def _force_stop(self) -> None:
        """Kill the sidecar without asking it nicely (startup failure path)."""
        proc, self.proc = self.proc, None
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=STOP_TIMEOUT_S)
            except (OSError, subprocess.TimeoutExpired):
                pass
            _close_streams(proc)
        self._close_log()
        self.url = ""
        self.screencast = False

    def _await_exit(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None:
            try:
                proc.wait(timeout=STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=STOP_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    pass
            # Close the pipes: without this every browser cycle leaks three
            # file descriptors, and a long-lived server eventually runs out.
            _close_streams(proc)
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=2.0)
            self._reader_thread = None
        self._close_log()
        with self._pending_lock:
            self._pending.clear()
        self.url = ""

    # -------------------------------------------------------------- state

    def _base_status(self) -> dict:
        return {
            "running": self.is_running(),
            "chat_id": self.chat_id,
            "url": self.url or None,
            "screencast": self.screencast,
            "started_at": self.started_at or None,
            "last_activity": self.last_activity or None,
            "frame_file": str(self.frame_file),
            "profile": str(self.profile),
        }

    def status(self) -> dict:
        """Live status dict for the UI routes and the browser_* tools.

        The live pane polls this once a second, so it must never sit behind
        a slow navigate holding the lifecycle lock: if the browser is busy,
        report the cached state and say so.
        """
        if not self.lock.acquire(timeout=0.75):
            base = self._base_status()
            base["busy"] = True
            if self.proc is not None:
                base["pid"] = self.proc.pid
            try:
                st = self.frame_file.stat()
                base["frame"] = {"mtime": st.st_mtime, "size": st.st_size}
            except OSError:
                base["frame"] = None
            return base
        try:
            base = self._base_status()
            if not base["running"]:
                return base
            base["pid"] = self.proc.pid
            try:
                res = self._send({"cmd": "status"}, timeout=15.0)
            except BrowserError as e:
                # A wedged sidecar must not make the whole status route hang
                # or 500: report what is known and say why it is partial.
                base["error"] = str(e)
            else:
                v = res.get("value") or {}
                base["url"] = v.get("url") or self.url
            try:
                st = self.frame_file.stat()
                base["frame"] = {"mtime": st.st_mtime, "size": st.st_size}
            except OSError:
                base["frame"] = None
            return base
        finally:
            self.lock.release()

    def frame_bytes(self) -> bytes | None:
        """Latest frame, or None if there is not a complete one yet.

        The sidecar rewrites frame.jpg in place, so a read can land midway
        through a write and return a truncated image. Retry briefly and only
        hand back data that starts and ends with the JPEG markers.
        """
        for attempt in range(3):
            try:
                data = self.frame_file.read_bytes()
            except OSError:
                return None
            if len(data) > 4 and data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9":
                return data
            if attempt < 2:
                time.sleep(0.05)
        return data or None

    def close(self) -> dict:
        with self.lock:
            was_running = self.is_running()
            if was_running:
                try:
                    self._send({"cmd": "close"}, timeout=STOP_TIMEOUT_S)
                except (BrowserError, OSError):
                    pass
                self._await_exit()
            else:
                # Never started, or already exited: still release any fds.
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
    """The browser for this chat.

    An already-registered browser is returned even when the session's meta
    has gone (deleted chat, moved workspace): otherwise its Chromium would
    be unreachable and keep running forever. Only *creating* one needs a
    live session, since that is what supplies the session folder.
    """
    with _registry_lock:
        b = _registry.get(chat_id)
    if b is not None:
        return b
    if not create:
        return None
    if ch.get_session(chat_id) is None:
        return None
    with _registry_lock:
        b = _registry.get(chat_id)
        if b is None:
            b = _Browser(chat_id, ch.session_dir(chat_id))
            _registry[chat_id] = b
        return b


def _forget(chat_id: str) -> None:
    with _registry_lock:
        _registry.pop(chat_id, None)


def _idle_watch() -> None:
    global _idle_thread
    if _idle_thread is None or not _idle_thread.is_alive():
        _idle_thread = threading.Thread(target=_idle_loop, daemon=True)
        _idle_thread.start()


def _idle_timeout() -> float:
    try:
        bcfg = cfg.load().get("browser") or {}
        return max(10.0, float(bcfg.get("idle_timeout_s", 300)))
    except (OSError, ValueError, TypeError):
        return 300.0


def _idle_loop() -> None:
    """Tear down browsers idle longer than browser.idle_timeout_s."""
    while True:
        time.sleep(IDLE_POLL_S)
        timeout = _idle_timeout()
        now = time.time()
        with _registry_lock:
            items = list(_registry.items())
        for chat_id, b in items:
            try:
                if b.is_running():
                    if b.last_activity and now - b.last_activity > timeout:
                        b.close()
                        _forget(chat_id)
                elif b.proc is None and b.started_at:
                    # already stopped: drop it so the registry does not grow
                    # without bound over the life of the server
                    _forget(chat_id)
            except Exception:
                continue


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
    try:
        return b.close()
    finally:
        _forget(chat_id)


def shutdown_all() -> None:
    """Tear down every sidecar (server shutdown hook)."""
    with _registry_lock:
        items = list(_registry.items())
    for chat_id, b in items:
        try:
            b.close()
        except Exception:
            pass
        _forget(chat_id)
