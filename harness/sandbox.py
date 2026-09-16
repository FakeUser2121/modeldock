"""Directory confinement + approval gate.

Every command the agent runs executes with cwd = the chat's workspace.
The sandbox decides, per command, one of:

    allow      -> execute it
    deny       -> refuse it (hard, no approval — mode violation)
    approval   -> needs the user's explicit yes (action leaves the workspace)

Modes:
    readonly    read-only; anything that writes or leaves the workspace is denied
    write       may write, but only inside the workspace; outside needs approval
    autonomous  inside the workspace always allowed; outside the workspace is
                allowed only if the exact command string is on the whitelist
                (allowed_outside_commands) or the user approves it.
    extend      same confinement as write, plus the harness's self-extension
                meta tool (extend_harness): the model can register new custom
                tools for the harness when the user asks. Sandbox semantics
                are identical to write; the extension capability is handled
                by tools/turn, not by the sandbox.
"""
import shlex
import subprocess
from pathlib import Path

MODES = ("readonly", "write", "autonomous", "extend")

# Commands that mutate state; these are refused outright in readonly mode.
WRITE_CMDS = {
    "rm", "rmdir", "mv", "cp", "mkdir", "touch", "chmod", "chown", "ln",
    "dd", "tee", "mkfs", "shred", "unlink", "install", "ln", "patch",
    "sed", "awk", "perl",  # in-place editors can mutate
    "python", "python3", "node", "sh", "bash",  # scripts can write
}
# Note: even allowed interpreters must keep their targets inside the
# workspace; the path check below still applies to them.

REDIR_MARKERS = (">", ">>", ">", ">|")
OUTSIDE_TIMEOUT = 60  # seconds, max wall time for one command


class SandboxError(Exception):
    pass


def parse_command(cmd: str) -> list[str]:
    try:
        return shlex.split(cmd)
    except ValueError as e:
        raise SandboxError(f"cannot parse command: {e}") from e


def _token_is_path(tok: str) -> bool:
    if tok.startswith("/"):
        return True
    if tok.startswith("~"):
        return True
    if "://" in tok:
        return False  # URL, not a local path
    # bare relative path-ish tokens are treated as inside the workspace
    return False


def _extract_paths(tokens: list[str], workspace: Path) -> list[Path]:
    """Best-effort extraction of filesystem paths referenced by a command."""
    paths: list[Path] = []
    for i, tok in enumerate(tokens):
        if tok in (">", ">>", ">|"):
            # the next token is a redirect target
            if i + 1 < len(tokens) and _token_is_path(tokens[i + 1]):
                p = Path(tokens[i + 1])
            elif i + 1 < len(tokens) and tokens[i + 1]:
                p = Path(tokens[i + 1])
            else:
                continue
            if not p.is_absolute():
                p = workspace / p
            paths.append(Path(p).expanduser())
            continue
        if tok.startswith("-"):
            # options; a later --flag=/path still handled by the '=' branch
            if "=" in tok:
                _, _, val = tok.partition("=")
                if _token_is_path(val):
                    p = Path(val).expanduser()
                    if not p.is_absolute():
                        p = workspace / p
                    paths.append(p)
            continue
        if _token_is_path(tok):
            p = Path(tok).expanduser()
            if not p.is_absolute():
                p = workspace / p
            paths.append(p)
    return paths


def is_outside(workspace: Path, p: Path) -> bool:
    try:
        p2 = Path(p).expanduser().resolve()
        ws2 = workspace.resolve()
    except (OSError, RuntimeError):
        return True
    if p2 == ws2:
        return False
    try:
        p2.relative_to(ws2)
        return False
    except ValueError:
        return True


def has_redirect(tokens: list[str]) -> bool:
    return any(t in (">", ">>", ">|") for t in tokens)


def _command_name(tokens: list[str]) -> str:
    if not tokens:
        return ""
    return Path(tokens[0]).name


def decide(mode: str, cmd: str, workspace: str, whitelist: list[str]) -> dict:
    """Pure decision. Returns one of:
        {"action": "allow"}
        {"action": "deny", "reason": str}
        {"action": "approval", "reason": str, "paths_outside": [str], "outside_paths": [str]}
    """
    if mode not in MODES:
        raise SandboxError(f"unknown mode: {mode}")
    tokens = parse_command(cmd)
    if not tokens:
        return {"action": "deny", "reason": "empty command"}
    ws = Path(workspace).resolve()

    # Normalize the command for whitelist comparison (collapse whitespace).
    norm = " ".join(tokens)

    paths = _extract_paths(tokens, ws)
    outside = [p for p in paths if is_outside(ws, p)]

    name = _command_name(tokens)
    leaves_workspace = bool(outside)

    # Whitelist: exact command string allowed even outside the workspace.
    if norm in (whitelist or []):
        return {"action": "allow"}

    if mode == "readonly":
        if name in WRITE_CMDS or has_redirect(tokens):
            return {"action": "deny", "reason": f"readonly mode: {name!r} would write"}
        if leaves_workspace:
            # readonly refuses to leave the workspace at all
            return {"action": "deny", "reason": "readonly mode: command leaves the workspace"}
        return {"action": "allow"}

    if mode in ("write", "extend"):
        if leaves_workspace:
            return {
                "action": "approval",
                "reason": "command references paths outside this chat's workspace",
                "paths_outside": [str(p) for p in outside],
            }
        return {"action": "allow"}

    # autonomous
    if leaves_workspace:
        return {
            "action": "approval",
            "reason": "command references paths outside this chat's workspace",
            "paths_outside": [str(p) for p in outside],
        }
    return {"action": "allow"}


def run_command(cmd: str, workspace: str, timeout: int = OUTSIDE_TIMEOUT) -> dict:
    """Execute a command with cwd = workspace. Returns a JSON-serializable
    result dict. The caller (execute_tool) is responsible for the sandbox
    decision; this only runs things it was told to run."""
    tokens = parse_command(cmd)
    ws = Path(workspace).resolve()
    if not ws.is_dir():
        return {"ok": False, "exit": None, "stdout": "", "stderr": f"workspace missing: {ws}"}
    try:
        proc = subprocess.run(
            tokens,
            cwd=str(ws),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit": None, "stdout": "", "stderr": f"timed out after {timeout}s"}
    except FileNotFoundError:
        return {"ok": False, "exit": None, "stdout": "", "stderr": f"command not found: {tokens[0]}"}
    except OSError as e:
        return {"ok": False, "exit": None, "stdout": "", "stderr": f"{e}"}
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    return {
        "ok": proc.returncode == 0,
        "exit": proc.returncode,
        "stdout": out[:8000],
        "stderr": err[:8000],
    }
