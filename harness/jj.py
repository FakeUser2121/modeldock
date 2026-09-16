"""Jujutsu (jj) fully-automatic version control per chat folder.

The harness manages jj on the user's behalf; the LLM never runs jj. For a
chat's workspace folder:
  * if no `.jj` exists, `jj git init --colocate` runs once (colocation is the
    default layout: `.jj` and `.git` side by side; also works on folders that
    already have a `.git`, importing the git history);
  * after each completed turn, working-copy changes are committed
    automatically with a message `chat <id> turn <n>: <user text…>`;
  * a per-chat bookmark `chat/<id>` is kept pointing at the chat's newest
    commit, so the graph shows one branch per chat.

The GUI (via the endpoints in main.py) lets the user see everything the
agent did and act on it: inspect any commit, view diffs, restore a
commit's file state (revert), edit an old commit, rewrite a message,
duplicate/abandon, manage bookmarks, and use the operation log (undo,
revert a specific op, restore the whole repo to an earlier op).

jj 0.44 CLI facts (verified on a scratch repo + docs/man pages):
  * `jj git init --colocate` is the repo initializer (`jj init` does not
    exist); `--colocate` is the default layout;
  * `jj log --template` supports `change_id.short()`, `author.name()`,
    `author.email()`, `author.timestamp().utc().format(...)`,
    `bookmarks.map(|r| r.name()).join(",")`, `self.diff().files().len()`,
    `parents.map(|c| c.change_id().short()).join(",")`,
    `description.replace(...)`, `++` concat, `"\t"`/`"\n"` escapes;
  * `jj log` prefixes every line with decoration (glyph + 2 spaces, or
    `│` / `├─╯` branch marks); the root commit has empty author/email;
  * `jj commit -m` on an empty working copy still creates an empty commit,
    so emptiness is checked first;
  * `jj restore --from <rev>` copies a revision's file state into the
    working copy (there is no `-r` flag for restore);
  * `jj diff -r <rev>` diffs a commit against its parent; `--from/--to`
    diff two arbitrary revisions; both support `--name-only`/`--summary`;
  * `jj show -r <rev>` prints `Commit ID`, `Change ID`, `Author`,
    `Committer` lines then the description;
  * `jj edit -r <rev>` moves the working copy to that commit for editing;
    a bare `jj commit` with no `-m` opens an editor, so always pass `-m`;
  * `jj describe -r <rev> -m <msg>` rewrites a commit message (rebased
    descendants); `jj duplicate -r`, `jj abandon -r` work on any commit;
  * `jj bookmark list` prints `name: change8 commit8 (flags) description`;
    `jj bookmark create <name>` makes a bookmark at @;
  * `jj op log` (default format) prints per op:
      `<glyph>  <opid 12hex> <user@host> <workspace> <relative time>, lasted N ms`
      followed by indented `│  <description>` and `│  args: <exact command>`
    lines;
  * `jj op revert <op>` reverts ONE earlier operation; `jj op restore <op>`
    restores the whole repo to an earlier operation; `jj undo` undoes the
    latest op.
"""
import re
import subprocess
import threading
from pathlib import Path

_JJ_BIN = "jj"

LOG_TEMPLATE = (
    "change_id.short() ++ \"\\t\" ++ author.name() ++ \"\\t\" ++ author.email() "
    "++ \"\\t\" ++ author.timestamp().utc().format(\"%Y-%m-%dT%H:%M:%SZ\") "
    "++ \"\\t\" ++ bookmarks.map(|r| r.name()).join(\",\") "
    "++ \"\\t\" ++ self.diff().files().len() "
    "++ \"\\t\" ++ parents.map(|c| c.change_id().short()).join(\",\") "
    "++ \"\\t\" ++ description.replace(\"\\n\", \" \") ++ \"\\n\""
)

_locks: dict[str, threading.Lock] = {}


def _lock(folder: Path) -> threading.Lock:
    key = str(folder)
    return _locks.setdefault(key, threading.Lock())


def _run(cmd: list[str], cwd: Path, timeout: float = 20.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
    )


def _err(r: subprocess.CompletedProcess, what: str) -> RuntimeError:
    return RuntimeError(f"{what} failed: {(r.stderr or r.stdout).strip()[:400]}")


def rev_of(folder: Path, expr: str) -> str:
    """Resolve a revset expression to a clean change id (strips decoration)."""
    with _lock(folder):
        r = _run(["jj", "log", "--no-pager", "-r", expr,
                   "--template", "change_id.short()"], folder)
    if r.returncode != 0:
        raise _err(r, f"resolve {expr!r}")
    m = re.search(r"\b[0-9a-z]{8,}\b", r.stdout)
    if not m:
        raise RuntimeError(f"could not resolve {expr!r}: {r.stdout.strip()[:200]}")
    return m.group(0)


def ensure_repo(folder: Path) -> bool:
    """Ensure `folder` is a colocated jj+git repo.

    Uses `jj git init --colocate` per the project jj guide: colocation is the
    default layout, so this works in a fresh folder (creates `.jj` and `.git`)
    AND in a folder with an existing `.git` (imports the git history).
    Returns True if a repo was created.
    """
    folder = Path(folder)
    with _lock(folder):
        if (folder / ".jj").exists():
            return False
        r = _run([_JJ_BIN, "git", "init", "--colocate"], folder)
    if r.returncode != 0 or not (folder / ".jj").exists():
        raise _err(r, "jj git init")
    return True


def working_copy_file_count(folder: Path) -> int:
    """Number of files changed in the working copy (0 = nothing to commit)."""
    folder = Path(folder)
    with _lock(folder):
        r = _run([_JJ_BIN, "log", "--no-pager", "-r", "@",
                  "--template", "self.diff().files().len()"], folder)
    if r.returncode != 0:
        raise _err(r, "jj status")
    m = re.search(r"\d+", r.stdout)
    if not m:
        raise RuntimeError(f"could not parse working-copy status: {r.stdout.strip()[:200]}")
    return int(m.group(0))


def commit(folder: Path, message: str) -> bool:
    """Commit the working copy. Returns False if there was nothing to commit."""
    folder = Path(folder)
    with _lock(folder):
        if working_copy_file_count_unlocked(folder) == 0:
            return False
        r = _run([_JJ_BIN, "commit", "-m", message], folder)
    if r.returncode != 0:
        raise _err(r, "jj commit")
    return True


def working_copy_file_count_unlocked(folder: Path) -> int:
    r = _run([_JJ_BIN, "log", "--no-pager", "-r", "@",
              "--template", "self.diff().files().len()"], folder)
    if r.returncode != 0:
        raise _err(r, "jj status")
    m = re.search(r"\d+", r.stdout)
    if not m:
        raise RuntimeError(f"could not parse working-copy status: {r.stdout.strip()[:200]}")
    return int(m.group(0))


def status(folder: Path) -> dict:
    """Current working-copy state: {change_id, commit_id, files}."""
    folder = Path(folder)
    with _lock(folder):
        r = _run([_JJ_BIN, "log", "--no-pager", "-r", "@",
                  "--template",
                  "change_id.short() ++ \"\\t\" ++ commit_id.short() ++ \"\\t\" "
                  "++ self.diff().files().len()"], folder)
    if r.returncode != 0:
        raise _err(r, "jj status")
    parts = r.stdout.strip().split("\t")
    m = re.search(r"\b[0-9a-z]{8,}\b", parts[0])
    if not m or len(parts) < 3:
        raise RuntimeError(f"could not parse jj status: {r.stdout.strip()[:200]}")
    return {"change_id": m.group(0), "commit_id": parts[1].strip(),
            "files": int(parts[2])}


def log(folder: Path) -> list[dict]:
    """Parse `jj log` for the whole history, newest first.

    Each entry: {id, author, email, time, bookmarks, files, parents,
    description}. `parents` is a list of parent change ids (for the graph).
    """
    folder = Path(folder)
    with _lock(folder):
        r = _run([_JJ_BIN, "log", "--no-pager", "-r", "all()",
                  "--template", LOG_TEMPLATE], folder)
    if r.returncode != 0:
        raise _err(r, "jj log")
    entries: list[dict] = []
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 7)  # description is last and may contain \t
        if len(parts) < 8:
            continue
        first = parts[0]
        m = re.search(r"\b[0-9a-z]{8,}\b", first)  # change id, skipping glyph prefix
        if not m:
            continue
        entries.append({
            "id": m.group(0),
            "author": parts[1],
            "email": parts[2],
            "time": parts[3],
            "bookmarks": [b for b in parts[4].split(",") if b],
            "files": int(parts[5]) if parts[5].isdigit() else 0,
            "parents": [p for p in parts[6].split(",") if p],
            "description": parts[7],
        })
    return entries


def show(folder: Path, rev: str) -> dict:
    """Parse `jj show -r <rev>`: {commit_id, change_id, author, email,
    time, committer, description}."""
    folder = Path(folder)
    with _lock(folder):
        r = _run([_JJ_BIN, "show", "--no-pager", "-r", rev], folder)
    if r.returncode != 0:
        raise _err(r, f"jj show {rev}")
    out = r.stdout
    commit_id = re.search(r"^Commit ID:\s*(\S+)", out, re.M)
    change_id = re.search(r"^Change ID:\s*(\S+)", out, re.M)
    author = re.search(r"^Author\s*:\s*(.+?)\s*<([^>]+)>\s*\(([^)]+)\)", out, re.M)
    committer = re.search(r"^Committer\s*:\s*(.+?)\s*<([^>]+)>\s*\(([^)]+)\)", out, re.M)
    desc = ""
    m = re.search(r"^\s*$\n(.*)$", out, re.S)
    if m:
        desc = m.group(1).strip()
    return {
        "commit_id": commit_id.group(1) if commit_id else "",
        "change_id": change_id.group(1) if change_id else "",
        "author": author.group(1) if author else "",
        "email": author.group(2) if author else "",
        "time": author.group(3) if author else "",
        "committer": committer.group(1) if committer else "",
        "description": desc,
    }


def files(folder: Path, rev: str) -> list[str]:
    """Paths changed between `rev` and its parent(s)."""
    folder = Path(folder)
    with _lock(folder):
        r = _run([_JJ_BIN, "diff", "--no-pager", "-r", rev, "--name-only"], folder)
    if r.returncode != 0:
        raise _err(r, f"jj diff {rev}")
    return [p for p in r.stdout.splitlines() if p.strip()]


def diff(folder: Path, rev: str | None = None,
         from_rev: str | None = None, to_rev: str | None = None) -> str:
    """Unified patch text. `rev`: commit vs parent; or `from_rev`/`to_rev`
    for two arbitrary revisions."""
    folder = Path(folder)
    if rev is not None:
        args = [_JJ_BIN, "diff", "--no-pager", "-r", rev]
    else:
        args = [_JJ_BIN, "diff", "--no-pager",
                "--from", from_rev or "@", "--to", to_rev or "@"]
    with _lock(folder):
        r = _run(args, folder)
    if r.returncode != 0:
        raise _err(r, "jj diff")
    return r.stdout


def restore(folder: Path, rev: str, commit_message: str | None = None) -> dict:
    """Copy `rev`'s file state into the working copy (a revert).

    If the working copy has changes after the restore and `commit_message`
    is given, commits them immediately and reports the new change id.
    """
    folder = Path(folder)
    with _lock(folder):
        r = _run([_JJ_BIN, "restore", "--from", rev], folder)
    if r.returncode != 0:
        raise _err(r, f"jj restore {rev}")
    result = {"restored_from": rev, "committed": False, "change_id": None}
    n = working_copy_file_count(folder)
    if n > 0 and commit_message:
        commit(folder, commit_message)
        result["committed"] = True
        result["change_id"] = status(folder)["change_id"]
    result["pending_files"] = working_copy_file_count(folder)
    return result


def undo(folder: Path) -> str:
    """Undo the latest operation. Returns jj's report text."""
    folder = Path(folder)
    with _lock(folder):
        r = _run([_JJ_BIN, "undo"], folder)
    if r.returncode != 0:
        raise _err(r, "jj undo")
    return (r.stderr or r.stdout).strip()


def op_log(folder: Path, limit: int = 100) -> list[dict]:
    """Parse `jj op log` (default format), newest first.

    Each entry: {id, user, workspace, time, description, args, local}.
    `args` is the exact command that ran (great for "what the agent did").
    """
    folder = Path(folder)
    with _lock(folder):
        r = _run([_JJ_BIN, "op", "log", "--no-pager", "--limit", str(limit)], folder)
    if r.returncode != 0:
        raise _err(r, "jj op log")
    header = re.compile(r"^\s*([○·@◆*+?])\s+([0-9a-f]{12})\s+(\S+)\s+(\S+)\s+(.*)$")
    entries: list[dict] = []
    for line in r.stdout.splitlines():
        m = header.match(line)
        if m:
            glyph, op_id, user, workspace, time = m.groups()
            entries.append({
                "id": op_id,
                "user": user,
                "workspace": workspace,
                "time": time,
                "description": "",
                "args": "",
                "local": glyph == "@",
            })
            continue
        m = re.match(r"^\s*[│|]?\s+(.*)$", line)
        if m and entries:
            text = m.group(1).strip()
            if not text:
                continue
            if text.startswith("args:"):
                entries[-1]["args"] = text[len("args:"):].strip()
            else:
                entries[-1]["description"] = (
                    (entries[-1]["description"] + " " if entries[-1]["description"] else "")
                    + text
                )
    return entries


def op_revert(folder: Path, op_id: str) -> str:
    """Revert one specific earlier operation (not just the latest)."""
    folder = Path(folder)
    with _lock(folder):
        r = _run([_JJ_BIN, "op", "revert", op_id], folder)
    if r.returncode != 0:
        raise _err(r, f"jj op revert {op_id}")
    return (r.stderr or r.stdout).strip()


def op_restore(folder: Path, op_id: str) -> str:
    """Restore the entire repo to how it looked at an earlier operation."""
    folder = Path(folder)
    with _lock(folder):
        r = _run([_JJ_BIN, "op", "restore", op_id], folder)
    if r.returncode != 0:
        raise _err(r, f"jj op restore {op_id}")
    return (r.stderr or r.stdout).strip()


def edit(folder: Path, rev: str) -> None:
    """Move the working copy to `rev` so it can be edited in place.

    The next `commit` (or the next turn's auto-commit) snapshots the edits
    into that commit.
    """
    folder = Path(folder)
    with _lock(folder):
        r = _run([_JJ_BIN, "edit", "-r", rev], folder)
    if r.returncode != 0:
        raise _err(r, f"jj edit {rev}")


def duplicate(folder: Path, rev: str) -> dict:
    """Create a duplicate commit of `rev` on top of the working copy's parent."""
    folder = Path(folder)
    with _lock(folder):
        r = _run([_JJ_BIN, "duplicate", "-r", rev], folder)
    if r.returncode != 0:
        raise _err(r, f"jj duplicate {rev}")
    m = re.search(r"as\s+([0-9a-z]{8,})", r.stderr or r.stdout)
    return {"duplicated_from": rev, "new_change_id": m.group(1) if m else None}


def abandon(folder: Path, rev: str) -> dict:
    """Abandon a commit (descendants are rebased onto its parent)."""
    folder = Path(folder)
    with _lock(folder):
        r = _run([_JJ_BIN, "abandon", "-r", rev], folder)
    if r.returncode != 0:
        raise _err(r, f"jj abandon {rev}")
    return {"abandoned": rev, "report": (r.stderr or r.stdout).strip()[:400]}


def describe(folder: Path, rev: str, message: str) -> None:
    """Rewrite the commit message of `rev` (descendants are rebased)."""
    folder = Path(folder)
    with _lock(folder):
        r = _run([_JJ_BIN, "describe", "-r", rev, "-m", message], folder)
    if r.returncode != 0:
        raise _err(r, f"jj describe {rev}")


def bookmark_list(folder: Path) -> dict[str, dict]:
    """All bookmarks: name -> {change_id, commit_id, description}."""
    folder = Path(folder)
    with _lock(folder):
        r = _run([_JJ_BIN, "bookmark", "list", "--no-pager"], folder)
    if r.returncode != 0:
        raise _err(r, "jj bookmark list")
    out: dict[str, dict] = {}
    for line in r.stdout.splitlines():
        if ": " not in line:
            continue
        name, rest = line.split(": ", 1)
        m = re.match(r"\s*([0-9a-z]{8,})\s+([0-9a-f]{8,})\s*(.*)$", rest)
        if not m:
            continue
        out[name] = {
            "change_id": m.group(1),
            "commit_id": m.group(2),
            "description": m.group(3).strip(),
        }
    return out


def bookmark_create(folder: Path, name: str, rev: str | None = None) -> None:
    """Create a bookmark (at `rev`, or at @ if omitted)."""
    folder = Path(folder)
    args = [_JJ_BIN, "bookmark", "create", name]
    if rev is not None:
        args += ["-r", rev]
    with _lock(folder):
        r = _run(args, folder)
    if r.returncode != 0:
        raise _err(r, f"jj bookmark create {name}")


def bookmark_move(folder: Path, name: str, rev: str) -> None:
    """Move an existing bookmark to `rev`."""
    folder = Path(folder)
    with _lock(folder):
        r = _run([_JJ_BIN, "bookmark", "move", name, "--to", rev], folder)
    if r.returncode != 0:
        raise _err(r, f"jj bookmark move {name}")


def bookmark_delete(folder: Path, name: str) -> None:
    folder = Path(folder)
    with _lock(folder):
        r = _run([_JJ_BIN, "bookmark", "delete", name], folder)
    if r.returncode != 0:
        raise _err(r, f"jj bookmark delete {name}")


def bookmark_upsert(folder: Path, name: str, rev: str) -> bool:
    """Point bookmark `name` at `rev`, creating it if needed. Best-effort."""
    try:
        bookmark_move(folder, name, rev)
        return True
    except (RuntimeError, OSError):
        pass
    try:
        bookmark_create(folder, name, rev)
        return True
    except (RuntimeError, OSError):
        return False
