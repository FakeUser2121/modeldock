"""Verify the jj commands the harness will run, on the scratch repo.

Run: ./.venv/bin/python tests/jj_verify.py
Focus: restore --from, diff -r / --from/--to, show, edit, evolog, duplicate,
abandon, describe, op log parsing, op revert, op restore.
"""
import re
import subprocess
from pathlib import Path

WS = Path("/home/user3/Documents/unsafe/.jj-verify")


def run(*args: str, cwd=WS, timeout=20.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(args), cwd=str(cwd), capture_output=True, text=True, timeout=timeout
    )


def show(title: str, r: subprocess.CompletedProcess) -> None:
    print(f"=== {title} === rc={r.returncode}")
    print((r.stdout or "").strip()[:2000])
    if (r.stderr or "").strip():
        print("[stderr]", (r.stderr or "").strip()[:500])
    print()


def rev(expr: str) -> str:
    """Get a clean change id for a revset expression (strip glyph prefix)."""
    r = run("jj", "log", "--no-pager", "-r", expr, "--template", "change_id.short()")
    if r.returncode != 0:
        raise RuntimeError(f"rev {expr}: {r.stderr.strip()[:300]}")
    m = re.search(r"\b([0-9a-z]{8,20})\b", r.stdout)
    if not m:
        raise RuntimeError(f"no change id in {r.stdout!r}")
    return m.group(1)


# start from a known state
show("current log", run("jj", "log", "--no-pager", "--template",
                        'change_id.short() ++ " | " ++ description.replace("\\n", " ") ++ "\\n"'))

older = rev("@--")
newer = rev("@-")

# 1. restore --from (older file state into working copy)
show(f"restore --from {older}", run("jj", "restore", "--from", older))
show("status after restore", run("jj", "st", "--no-pager"))
show("wc file count after restore", run("jj", "log", "--no-pager", "-r", "@",
                                        "--template", "self.diff().files().len()"))
show("commit the restore", run("jj", "commit", "-m", f"restore state from {older}"))

# 2. diff -r (commit vs parent), full patch
show(f"diff -r {newer}", run("jj", "diff", "--no-pager", "-r", newer))

# 3. diff --from/--to between two revisions
show(f"diff --from {older} --to {newer} --name-only",
     run("jj", "diff", "--no-pager", "--from", older, "--to", newer, "--name-only"))
show(f"diff --from {older} --to {newer} --summary",
     run("jj", "diff", "--no-pager", "--from", older, "--to", newer, "--summary"))

# 4. show -r (full commit display)
show(f"show -r {newer}", run("jj", "show", "--no-pager", "-r", newer))

# 5. edit -r (move working copy to edit an older commit)
target = rev("@--")
show(f"edit -r {target}", run("jj", "edit", "-r", target))
show("status after edit", run("jj", "st", "--no-pager"))
show("close edit (commit)", run("jj", "commit"))

# 6. evolog -r
target = rev("@-")
show(f"evolog -r {target}", run("jj", "evolog", "--no-pager", "-r", target))

# 7. duplicate + abandon a leaf
show("duplicate @-", run("jj", "duplicate", "-r", "@-"))
dup = rev("@")
show(f"abandon {dup}", run("jj", "abandon", "-r", dup))

# 8. describe -r
target = rev("@-")
show(f"describe -r {target} -m", run("jj", "describe", "-r", target, "-m", "rewritten message"))

# 9. op log default output (table) — inspect format
show("op log (default, limit 6)", run("jj", "op", "log", "--no-pager", "--limit", "6"))

# 10. op revert a specific older op (then undo it)
r = run("jj", "op", "log", "--no-pager", "--limit", "3")
print(r.stdout)
op_ids = re.findall(r"^\S+\s+([0-9a-f]{8,})", r.stdout, re.M)
if len(op_ids) >= 2:
    old_op = op_ids[-1]
    show(f"op revert {old_op}", run("jj", "op", "revert", old_op))
    show("op log after revert", run("jj", "op", "log", "--no-pager", "--limit", "4"))

# 11. op restore an older op (repo state), then undo
r = run("jj", "op", "log", "--no-pager", "--limit", "3")
op_ids = re.findall(r"^\S+\s+([0-9a-f]{8,})", r.stdout, re.M)
if op_ids:
    old_op = op_ids[-1]
    show(f"op restore {old_op}", run("jj", "op", "restore", old_op))
    show("status after op restore", run("jj", "st", "--no-pager"))
