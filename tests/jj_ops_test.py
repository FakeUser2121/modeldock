"""Exercise every public function of harness.jj against the scratch repo.

Run: ./.venv/bin/python tests/jj_ops_test.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import jj

WS = Path("/home/user3/Documents/unsafe/.jj-verify")
ok = 0


def check(name, fn):
    global ok
    try:
        r = fn()
        ok += 1
        print(f"PASS {name}: {r if not isinstance(r, (list, dict)) else repr(r)[:200]}")
    except Exception as e:
        print(f"FAIL {name}: {e}")


check("ensure_repo (exists)", lambda: jj.ensure_repo(WS))
check("status", lambda: jj.status(WS))
check("log (has parents)", lambda: jj.log(WS)[:3])
root = jj.log(WS)[-1]["id"]
first = jj.log(WS)[-2]["id"]
check("rev_of @-", lambda: jj.rev_of(WS, "@-"))
check("show", lambda: jj.show(WS, first))
check("files", lambda: jj.files(WS, first))
check("diff single", lambda: jj.diff(WS, rev=first)[:120])
check("diff from/to name-only", lambda: jj.diff(WS, from_rev=root, to_rev=first).strip()[:120])
check("restore --from + auto commit",
      lambda: jj.restore(WS, first, commit_message=f"revert to {first} (test)"))
check("describe", lambda: (jj.describe(WS, first, "rewritten by test"), "ok")[1])
check("duplicate", lambda: jj.duplicate(WS, first))
check("edit (move wc)", lambda: (jj.edit(WS, first), "ok")[1])
check("commit after edit (empty -> False)", lambda: jj.commit(WS, "should be empty"))
check("bookmark list", lambda: jj.bookmark_list(WS))
check("bookmark upsert chat/x", lambda: jj.bookmark_upsert(WS, "chat/x", first))
check("op_log", lambda: jj.op_log(WS, limit=5))
ops = jj.op_log(WS, limit=3)
check("op_revert (oldest of 3)", lambda: jj.op_revert(WS, ops[-1]["id"]))
check("op_restore (oldest of 3)", lambda: jj.op_restore(WS, ops[-1]["id"]))
check("undo", lambda: jj.undo(WS))
check("bookmark_delete", lambda: (jj.bookmark_delete(WS, "chat/x"), "ok")[1])

print(f"\n{ok} passed, {17 - ok} failed")
