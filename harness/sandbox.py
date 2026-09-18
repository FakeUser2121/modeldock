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
import os
import shlex
import signal
import subprocess
from pathlib import Path

MODES = ("readonly", "write", "autonomous", "extend")

# Shell control operators. A command string is split on these into segments,
# and every segment is judged on its own: `echo ok && rm -rf ~/x` must not
# pass just because its first segment is harmless.
OPERATORS = ("|", "||", "&&", "&", ";", "(", ")", "\n")
REDIRECTS = (">", ">>", ">|", "<", "<<", "2>", "&>")

# Interpreters that take a program on the command line. Their payload is code,
# not a path, so the plain token scan below cannot see what it touches --
# `bash -c 'rm -rf /etc'` looks like three harmless tokens. The payload is
# scanned separately (see `_paths_in_code`).
INTERPRETER_FLAGS = {
    "sh": ("-c",), "bash": ("-c",), "zsh": ("-c",), "dash": ("-c",), "ksh": ("-c",),
    "python": ("-c",), "python3": ("-c",), "node": ("-e", "--eval", "-p"),
    "perl": ("-e",), "ruby": ("-e",), "php": ("-r",),
}

# Commands that mutate state; these are refused outright in readonly mode.
WRITE_CMDS = {
    "rm", "rmdir", "mv", "cp", "mkdir", "touch", "chmod", "chown", "ln",
    "dd", "tee", "mkfs", "shred", "unlink", "install", "ln", "patch",
    "sed", "awk", "perl",  # in-place editors can mutate
    "python", "python3", "node", "sh", "bash",  # scripts can write
}
# Note: even allowed interpreters must keep their targets inside the
# workspace; the path check below still applies to them.

OUTSIDE_TIMEOUT = 60  # seconds, max wall time for one command
MAX_OUTPUT = 8000     # chars kept per stream before truncation


class SandboxError(Exception):
    pass


def parse_command(cmd: str) -> list[str]:
    """Tokenize a command, keeping shell operators as their own tokens.

    Unlike a plain `shlex.split`, `a && b` yields ['a', '&&', 'b'] so the
    decision layer can see the second command at all.
    """
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        return list(lex)
    except ValueError as e:
        raise SandboxError(f"cannot parse command: {e}") from e


def split_segments(tokens: list[str]) -> list[list[str]]:
    """Split an operator-aware token list into individual command segments."""
    segments: list[list[str]] = []
    cur: list[str] = []
    for tok in tokens:
        if tok in OPERATORS:
            if cur:
                segments.append(cur)
            cur = []
            continue
        cur.append(tok)
    if cur:
        segments.append(cur)
    return segments


def _token_is_path(tok: str) -> bool:
    """Does this token name a filesystem location?

    The old rule only recognised tokens starting with '/' or '~', so every
    relative escape (`cat ../../../etc/passwd`) read as "not a path" and
    sailed through the confinement check. Anything containing a separator,
    or that is exactly '.' or '..', counts now.
    """
    if not tok or tok.startswith("-"):
        return False
    if "://" in tok:
        return False  # URL, not a local path
    if tok.startswith("/") or tok.startswith("~"):
        return True
    if tok in (".", ".."):
        return True
    if "/" in tok:
        return True
    return False


def _resolve(tok: str, workspace: Path) -> Path:
    p = Path(tok).expanduser()
    if not p.is_absolute():
        p = workspace / p
    return p


def _paths_in_code(payload: str, workspace: Path) -> list[Path]:
    """Path-ish literals inside an interpreter payload (`bash -c '...'`).

    Code cannot be parsed reliably here, so this is deliberately blunt: any
    whitespace- or quote-delimited run of characters that looks like a path
    is extracted and checked. False positives cost an approval prompt;
    missing one costs an unnoticed escape from the workspace.
    """
    out: list[Path] = []
    for raw in payload.replace("'", " ").replace('"', " ").replace(";", " ").split():
        tok = raw.strip("(),")
        if _token_is_path(tok):
            out.append(_resolve(tok, workspace))
    return out


def _extract_paths(tokens: list[str], workspace: Path) -> list[Path]:
    """Best-effort extraction of filesystem paths referenced by a command."""
    paths: list[Path] = []
    name = _command_name(tokens)
    interp_flags = INTERPRETER_FLAGS.get(name, ())
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in REDIRECTS:
            # the next token is the redirect target, path-looking or not
            if i + 1 < len(tokens) and tokens[i + 1] and tokens[i + 1] not in OPERATORS:
                paths.append(_resolve(tokens[i + 1], workspace))
                i += 2
                continue
            i += 1
            continue
        if interp_flags and tok in interp_flags:
            # everything the interpreter is asked to execute
            if i + 1 < len(tokens):
                paths.extend(_paths_in_code(tokens[i + 1], workspace))
                i += 2
                continue
            i += 1
            continue
        if tok.startswith("-"):
            # options; a --flag=/path value is still a path
            if "=" in tok:
                _, _, val = tok.partition("=")
                if _token_is_path(val):
                    paths.append(_resolve(val, workspace))
            i += 1
            continue
        if _token_is_path(tok):
            paths.append(_resolve(tok, workspace))
        i += 1
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
    return any(t in REDIRECTS for t in tokens)


def _command_name(tokens: list[str]) -> str:
    if not tokens:
        return ""
    for tok in tokens:
        if "=" in tok and not tok.startswith("-") and "/" not in tok.split("=", 1)[0]:
            continue  # leading VAR=value assignment, not the command
        return Path(tok).name
    return ""


def _is_dynamic(tok: str) -> bool:
    """Token whose real value is only known once the shell expands it."""
    return "$" in tok or "`" in tok


def _substitution_bodies(cmd: str) -> list[str]:
    """Text inside $( ... ) and ` ... ` command substitutions."""
    out: list[str] = []
    i = 0
    while i < len(cmd) - 1:
        if cmd[i] == "$" and cmd[i + 1] == "(":
            depth, j = 1, i + 2
            while j < len(cmd) and depth:
                if cmd[j] == "(":
                    depth += 1
                elif cmd[j] == ")":
                    depth -= 1
                j += 1
            out.append(cmd[i + 2 : j - 1])
            i = j
            continue
        if cmd[i] == "`":
            j = cmd.find("`", i + 1)
            if j == -1:
                break
            out.append(cmd[i + 1 : j])
            i = j + 1
            continue
        i += 1
    return out


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

    # Whitelist: exact command string allowed even outside the workspace.
    if norm in (whitelist or []) or cmd.strip() in (whitelist or []):
        return {"action": "allow"}

    # Judge every segment of the pipeline, not just the first one.
    segments = split_segments(tokens)
    outside: list[Path] = []
    unresolvable: list[str] = []
    write_names: list[str] = []
    for seg in segments:
        for p in _extract_paths(seg, ws):
            if is_outside(ws, p):
                outside.append(p)
        for tok in seg:
            if _is_dynamic(tok) and _token_is_path(tok):
                # e.g. "$HOME/.ssh/id_rsa": the shell decides where this lands,
                # so it cannot be cleared statically.
                unresolvable.append(tok)
        seg_name = _command_name(seg)
        if seg_name in WRITE_CMDS:
            write_names.append(seg_name)

    # Text inside $( ) / backticks is executed too.
    for body in _substitution_bodies(cmd):
        for p in _paths_in_code(body, ws):
            if is_outside(ws, p):
                outside.append(p)

    leaves_workspace = bool(outside) or bool(unresolvable)
    reason = "command references paths outside this chat's workspace"
    if not outside and unresolvable:
        reason = (
            "command contains a shell-expanded path that cannot be checked "
            f"before it runs ({', '.join(sorted(set(unresolvable))[:3])})"
        )

    if mode == "readonly":
        if write_names or has_redirect(tokens):
            what = write_names[0] if write_names else "redirection"
            return {"action": "deny", "reason": f"readonly mode: {what!r} would write"}
        if leaves_workspace:
            # readonly refuses to leave the workspace at all
            return {"action": "deny", "reason": "readonly mode: command leaves the workspace"}
        return {"action": "allow"}

    # write / extend / autonomous: inside is free, outside needs a human yes.
    if leaves_workspace:
        return {
            "action": "approval",
            "reason": reason,
            "paths_outside": sorted({str(p) for p in outside}) + sorted(set(unresolvable)),
        }
    return {"action": "allow"}


def run_command(cmd: str, workspace: str, timeout: int = OUTSIDE_TIMEOUT) -> dict:
    """Execute a command with cwd = workspace. Returns a JSON-serializable
    result dict. The caller (execute_tool) is responsible for the sandbox
    decision; this only runs things it was told to run.

    The command runs under /bin/sh, so pipes, redirection, `&&`, globs and
    variable expansion behave the way the model (and the tool description)
    expects. Running the bare argv instead used to hand `>` and `|` to the
    program as literal arguments: `echo hi > out.txt` printed "hi > out.txt"
    and exited 0, i.e. reported success for a file it never wrote. Every
    segment of the command has already been through `decide()`.

    The child gets its own process group so a timeout can take down the whole
    pipeline; killing only the shell leaves its children running.
    """
    ws = Path(workspace).resolve()
    if not ws.is_dir():
        return {"ok": False, "exit": None, "stdout": "", "stderr": f"workspace missing: {ws}"}
    # Surface a parse error before handing the string to the shell.
    parse_command(cmd)
    env = dict(os.environ)
    env.setdefault("PWD", str(ws))
    try:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            executable="/bin/sh",
            cwd=str(ws),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,
        )
    except OSError as e:
        return {"ok": False, "exit": None, "stdout": "", "stderr": f"{e}"}
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        out, err = proc.communicate()
        return {
            "ok": False,
            "exit": None,
            "stdout": _cap(out),
            "stderr": _cap(err) or f"timed out after {timeout}s",
            "timed_out": True,
        }
    return {
        "ok": proc.returncode == 0,
        "exit": proc.returncode,
        "stdout": _cap(out),
        "stderr": _cap(err),
    }


def _cap(text: str | None) -> str:
    s = (text or "").strip()
    if len(s) <= MAX_OUTPUT:
        return s
    return s[:MAX_OUTPUT] + f"\n... [truncated, {len(s) - MAX_OUTPUT} more chars]"


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGTERM then SIGKILL the child's whole process group."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        try:
            proc.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            continue
