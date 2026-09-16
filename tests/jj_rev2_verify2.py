"""Round 2: refusal message, multi-ws @ markers, divergent bookmarks, squash opts."""
import re
import subprocess
from pathlib import Path

ROOT = Path("/home/user3/Documents/unsafe/.jj-rev2")
WS2 = ROOT / ".modeldock" / "workspaces" / "chatb"
ENV = {"XDG_CONFIG_HOME": str(ROOT / ".modeldock" / "jj-home")}

def run(*args, cwd=ROOT, check=True):
    import os
    env = {**os.environ, **ENV}
    r = subprocess.run(list(args), cwd=str(cwd), capture_output=True, text=True, timeout=30, env=env)
    return {"out": r.stdout, "err": r.stderr, "code": r.returncode}

def show(title, r):
    print(f"\n=== {title} (exit {r['code']}) ===")
    print((r["out"] or r["err"]).strip()[:1200])

# config get plain
show("config get", run("jj", "config", "get", "snapshot.max-new-file-size", check=False))

# refusal message with tiny limit
show("set limit 1KiB", run("jj", "config", "set", "--repo", "snapshot.max-new-file-size", "1KiB"))
(ROOT / "big.txt").write_text("x" * 5000)
show("st refusal", run("jj", "st", "--no-pager", check=False))
show("commit refusal", run("jj", "commit", "-m", "x", check=False))
show("undo refusal?", run("jj", "undo", check=False))
(ROOT / "big.txt").unlink()
show("set limit 64MiB", run("jj", "config", "set", "--repo", "snapshot.max-new-file-size", "64MiB"))

# multi-workspace @ markers in log
show("log (both ws)", run("jj", "log", "--no-pager", "--template", 'change_id.short() ++ " " ++ description'))
show("log json (both ws)", run("jj", "log", "--no-graph", "--template", 'json(self) ++ "\\n"', check=False))

# divergent bookmark: set same bookmark name from second workspace
run("jj", "bookmark", "set", "chat/t1", "-r", "@-", cwd=ROOT)
show("ws2 bookmark set chat/t1 (conflict?)", run("jj", "bookmark", "set", "chat/t1", "-r", "@-", cwd=WS2, check=False))
show("bookmark list (divergent?)", run("jj", "bookmark", "list", "--no-pager", check=False))
show("bookmark set --allow-backwards", run("jj", "bookmark", "set", "feat/x", "-r", "@-", "--allow-backwards", check=False))

# squash full help
show("squash --help (full)", run("jj", "squash", "--help", check=False))

# op abandon non-current
ops = run("jj", "op", "log", "--no-pager", "--limit", "10")
ids = re.findall(r"\b[0-9a-f]{12}\b", ops["out"])
if len(ids) >= 2:
    target = ids[-1]
    show(f"op abandon {target}", run("jj", "op", "abandon", target, check=False))

# workspace update-stale: make stale by committing in other ws? Try help + run
show("workspace update-stale --help", run("jj", "workspace", "update-stale", "--help", check=False))
show("workspace forget --help", run("jj", "workspace", "forget", "--help", check=False))
print("\nDONE")
