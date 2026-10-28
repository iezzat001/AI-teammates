#!/usr/bin/env python3
"""SessionStart - load the standing rules so they cannot be "forgotten".

The failure this fixes: rules were agreed, written to markdown, and then broken
anyway, because reading them was optional. This prints them into context at the
start of every session. It is not a suggestion the agent may skip.

FOUND 2026-09-19: this printed one project's hardcoded rules into EVERY project,
and they contradicted what the hooks enforce - "feature -> main", "docs direct
to main", a model that never existed, a retired memory store. The one doc an
agent cannot skip was the stalest. So the generic rules are now DERIVED from
config.json and the agent specs - the same sources the hooks enforce from - and
anything project-specific lives in .ai-teammates/RULES.md, which the project
owns and install.sh never overwrites.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _config  # noqa: E402
import seat  # noqa: E402

REPO = _config.repo_root()


def git(*args):
    try:
        return subprocess.run((_config.GIT,) + args, cwd=REPO, capture_output=True,
                              text=True, timeout=10).stdout.strip()
    except Exception:
        return "?"


def main():
    # The baseline for memlife's fact review: where the integration branch stood when this session
    # began. Never allowed to break the session.
    try:
        import memlife
        memlife.session_begin(str(memlife._hook_payload().get("session_id") or ""))
    except Exception:
        pass
    production, integration = _config.branches()
    flow = f"feature -> {integration} -> {production}" if integration else f"feature -> {production}"
    print(f"""
STANDING RULES (loaded by hook from config.json + agent specs, not optional)

BRANCHES
  Flow: {flow}. Nothing is authored on {production}; it advances only by merge.
  Guarded (product) paths: {', '.join(_config.guarded())} - edited on a feature branch.
  Pushes are judged by the git pre-push hook, which sees where they really land.
  Override a push only out loud: AI_TEAMMATES_ALLOW='<why>' git push ... (logged).
  Never `git branch -D` without `git log --oneline <keep>..<delete>` showing 0.

SEATS (from .ai-teammates/agents - launch with seat.py run <name> "<task>")""")
    for name, a in seat.load_agents().items():
        if name != "cto":
            print(f"  {name:<18} {a.get('harness', '?'):<11} {a.get('model', '?')}"
                  f"{'  read-only' if a.get('read_only') == 'true' else ''}")
    print("""
VERIFY, DO NOT ASSERT
  Check the output before saying it worked. Compiling proves the file parses,
  not that the patch applied - grep for the new symbol. Liveness is the process
  table, never a log. A delegated run is done when the diff exists.""")

    rules = REPO / ".ai-teammates" / "RULES.md"
    if rules.exists():
        print("\nPROJECT RULES (.ai-teammates/RULES.md)\n")
        print(rules.read_text().strip())

    # Asks that earlier runs (a headless `cto "task"`, a seat) could not get
    # answered. Shown first so they are the first task, not buried history.
    try:
        import loopguard
        pending = loopguard.unresolved()
    except Exception:
        pending = []
    if pending:
        print("\nBLOCKED, WAITING ON THE HUMAN (resolve these first)")
        for d in pending[-5:]:
            who = d["who"] + (f" on card {d['card']}" if d.get("card") else "")
            print(f"  [{d['class']}] {who} {d['ts'][:16]}Z - needs: {d['need']}")
        print("  Once the human has dealt with them: python3 .ai-teammates/hooks/loopguard.py resolve")

    print("\nREPO STATE")
    print("  branch: %s" % git("rev-parse", "--abbrev-ref", "HEAD"))
    print("  head:   %s" % git("log", "--oneline", "-1"))
    dirty = git("status", "--porcelain")
    print("  dirty:  %s file(s)" % (len(dirty.splitlines()) if dirty else 0))
    sys.exit(0)


if __name__ == "__main__":
    main()
