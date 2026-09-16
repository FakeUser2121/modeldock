"""Round 3: stale-ws recovery, divergent bookmark format, workspace list, user identity."""
import os
import re
import subprocess
from pathlib import Path

ROOT = Path("/home/user3/Documents/unsafe/.jj-rev2")
WS2 = ROOT / ".modeldock" / "workspaces" / "chatb"
ENV = {"XDG_CONFIG_HOME": str(ROOT / ".modeldock" / "jj-home")}

def run(*args, cwd=ROOT, check=True):
    env = {**os.environ, **ENV}
    r = subprocess.run(list(args), cwd=str(cwd), capture_output=True, text=True, timeout=30, env=env)
    return {"out": r.stdout, "err": r.stderr, "code": r.returncode}

def show(title, r):
    print(f"\n=== {title} (exit {r['code']}) ===")
    print((r["out"] or r["err"]).strip()[:1000])

# user identity config
show("set user.name", run("jj", "config", "set", "--user", "user.name", "ModelDock Agent", check=False))
show("set user.email", run("jj", "config", "set", "--user", "user.email", "agent@modeldock.local", check=False))

# 1. stale ws2 recovery
show("ws2 status (stale?)", run("jj", "status", "--no-pager", cwd=WS2, check=False))
show("ws2 workspace update-stale", run("jj", "workspace", "update-stale", cwd=WS2, check=False))
show("ws2 status after", run("jj", "status", "--no-pager", cwd=WS2, check=False))
show("ws2 op integrate?", run("jj", "op", "integrate", "--help", check=False))

# 2. full workspace list
show("workspace list full", run("jj", "workspace", "list", "--no-pager", check=False))

# 3. working_copies template property
show("log with working_copies", run("jj", "log", "--no-graph", "--template",
      'change_id.short() ++ " " ++ working_copies.map(|wc| wc.name()).join(",") ++ " " ++ description',
      check=False))

# 4. divergent bookmark via concurrent rewrite of the SAME commit from two workspaces
log = run("jj", "log", "--no-graph", "--template", 'change_id.short() ++ " " ++ description')
m = re.search(r"([a-z0-9]{8}) chat t1 turn 1", log["out"])
common = m.group(1) if m else None
print("\ncommon rev:", common)
show("set bookmark on common", run("jj", "bookmark", "set", "test/conv", "-r", common, cwd=ROOT, check=False))
show("ws1 describe common", run("jj", "describe", "-r", common, "-m", "ws1 rewrite", cwd=ROOT, check=False))
show("ws2 describe common", run("jj", "describe", "-r", common, "-m", "ws2 rewrite", cwd=WS2, check=False))
show("bookmark list after concurrent", run("jj", "bookmark", "list", "--no-pager", check=False))
show("log after concurrent", run("jj", "log", "--no-pager", "--template", 'change_id.short() ++ " " ++ description', check=False))

# make a bookmark point at a possibly-divergent change: set from ws2 to ws1's version
show("ws2 set chat/t1 allow-backwards", run("jj", "bookmark", "set", "chat/t1", "-r", "@-", "--allow-backwards", cwd=WS2, check=False))
show("bookmark list after set", run("jj", "bookmark", "list", "--no-pager", check=False))
show("status after", run("jj", "status", "--no-pager", check=False))
print("\nDONE")
