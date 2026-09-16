"""Verify the revised-plan jj commands against installed jj (0.44).

Sections:
 1. --config ui.paginate=never global flag
 2. jj workspace add / list / update-stale
 3. jj bookmark set / track
 4. jj config set --repo snapshot.max-new-file-size + refusal message format
 5. jj op abandon / jj util gc
 6. json(self) NDJSON templates for log and op log
 7. jj squash (non-interactive)
 8. divergent bookmark creation + detection in bookmark list
Run: ./.venv/bin/python tests/jj_rev2_verify.py
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path("/home/user3/Documents/unsafe/.jj-rev2")
if ROOT.exists():
    import shutil
    shutil.rmtree(ROOT)
ROOT.mkdir(parents=True)

def run(*args, cwd=ROOT, check=True):
    r = subprocess.run(list(args), cwd=str(cwd), capture_output=True, text=True, timeout=30)
    if check and r.returncode != 0:
        return {"out": r.stdout, "err": r.stderr, "code": r.returncode}
    return {"out": r.stdout, "err": r.stderr, "code": r.returncode}

def show(title, r):
    print(f"\n=== {title} (exit {r['code']}) ===")
    print((r["out"] or r["err"]).strip()[:900])

# 0. init colocated
show("init", run("jj", "git", "init", "--colocate"))
(ROOT / "a.txt").write_text("a1\n")
show("commit a", run("jj", "commit", "-m", "chat t1 turn 1: hello"))

# 1. --config global flag
show("global --config ui.paginate=never", run("jj", "--config", "ui.paginate=never", "log", "--no-pager", "-r", "@", "--template", "change_id.short()"))

# 2. workspaces
ws2 = ROOT / ".modeldock" / "workspaces" / "chatb"
ws2.mkdir(parents=True)
show("workspace add --help", run("jj", "workspace", "add", "--help", check=False))
show("workspace add", run("jj", "workspace", "add", str(ws2), "--name", "chat/chatb"))
show("workspace list", run("jj", "workspace", "list"))
(ws2 / "b.txt").write_text("b1\n")
show("commit in ws2", run("jj", "commit", "-m", "chat chatb turn 1: hi", cwd=ws2))
show("log shows both @", run("jj", "log", "--no-pager", "--template", "change_id.short() ++ \" \" ++ description"))
show("workspace update-stale --help", run("jj", "workspace", "update-stale", "--help", check=False))

# 3. bookmark set / track
show("bookmark set (create)", run("jj", "bookmark", "set", "feat/x", "-r", "@"))
show("bookmark set (move)", run("jj", "bookmark", "set", "feat/x", "-r", "@-"))
show("bookmark track --help", run("jj", "bookmark", "track", "--help", check=False))

# 4. snapshot size
show("config set limit 1KiB", run("jj", "config", "set", "--repo", "snapshot.max-new-file-size", "1KiB"))
show("config get limit", run("jj", "config", "get", "--repo", "snapshot.max-new-file-size"))
(ROOT / "big.txt").write_text("x" * 5000)
show("st with oversized file", run("jj", "st", "--no-pager", check=False))
show("log with oversized file", run("jj", "log", "--no-pager", "-r", "@", "--template", "self.diff().files().len()", check=False))
(ROOT / "big.txt").unlink()
show("config set limit 64MiB", run("jj", "config", "set", "--repo", "snapshot.max-new-file-size", "64MiB"))

# 5. op abandon / gc
ops = run("jj", "op", "log", "--no-pager", "--limit", "3")
print("\n=== op log (3) ===")
print(ops["out"][:400])
m = re.search(r"[0-9a-f]{12}", ops["out"])
oldest = m.group(0) if m else None
if oldest:
    show(f"op abandon {oldest}", run("jj", "op", "abandon", oldest, check=False))
show("util gc", run("jj", "util", "gc", check=False))

# 6. NDJSON templates
show("log json(self)", run("jj", "log", "--no-graph", "--template", 'json(self) ++ "\\n"', check=False))
show("op log json(self)", run("jj", "op", "log", "--template", 'json(self) ++ "\\n"', "--limit", "2", check=False))

# 7. squash
(ROOT / "c.txt").write_text("c1\n")
run("jj", "commit", "-m", "chat t1 turn 2: second")
show("squash --help", run("jj", "squash", "--help", check=False))
show("squash @", run("jj", "squash", "-r", "@", check=False))
show("log after squash", run("jj", "log", "--no-pager", "--template", "change_id.short() ++ \" \" ++ description"))

# 8. divergent bookmark
(ROOT / "d.txt").write_text("d1\n")
run("jj", "commit", "-m", "chat t1 turn 3: third")
run("jj", "bookmark", "set", "chat/t1", "-r", "@")
run("jj", "describe", "-r", "@", "-m", "rewritten")  # rewrite the commit the bookmark tracks
show("bookmark list after rewrite (divergent?)", run("jj", "bookmark", "list", "--no-pager", check=False))
show("status", run("jj", "st", "--no-pager", check=False))
print("\nDONE")
