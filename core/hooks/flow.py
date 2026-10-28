#!/usr/bin/env python3
"""The one invariant that encodes the branch flow:

    feature branch  ->  development  ->  main

    ==> `development` must ALWAYS contain everything `main` has.
    ==> `git rev-list --count development..main` must be 0.

If that number is not zero, something reached main without passing through
development. It does not matter what kind of file it was.

WHY THIS EXISTS (2026-09-10). The flow was the project's oldest and most
obvious rule, and it broke five times in one day. Not because anyone disagreed
with it - because nothing checked it. prepush.py enforced "product code never
lands directly on a protected branch" and explicitly allowed config and docs
straight to main. So every bypass was APPROVED as it happened:

    69b6b40  hooks: consolidate onto .ai-teammates       BYPASSED development
    1d9beb2  chore(session): commit the session record   BYPASSED development
    0fbc0b5  chore(session): commit the session record   BYPASSED development
    c8f21c3  dash: prune retired seats                   BYPASSED development
    f8f3802  memory: retire the hand-rolled stack        BYPASSED development

Every one was config or docs. prepush waved each one through, correctly by its
own rule, and the flow rule was violated five times with a green light.

The lesson is not "add another rule". It is that a rule phrased as a property of
FILES cannot enforce a property of DIRECTION. This file checks direction.

    python3 flow.py            # report
    python3 flow.py --fix      # fast-forward development to main, restoring it
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _config  # noqa: E402

GIT = _config.GIT
REPO = _config.repo_root()


def sh(*args):
    return subprocess.run([GIT, *args], cwd=REPO,
                          capture_output=True, text=True).stdout.strip()


def exists(ref: str) -> bool:
    return subprocess.run([GIT, "rev-parse", "--verify", "--quiet", ref],
                          cwd=REPO, capture_output=True).returncode == 0


def main() -> int:
    production, integration = _config.branches()

    if not integration or not exists(integration) or not exists(production):
        print(f"  flow: no separate integration branch configured — nothing to check")
        return 0

    bypassed = [l for l in sh("log", "--first-parent", "--format=%h %s",
                              f"{integration}..{production}").splitlines() if l]

    if not bypassed:
        ahead = sh("rev-list", "--count", f"{production}..{integration}")
        print(f"  flow  ok    {integration} contains {production}"
              f"  ({ahead} commit(s) waiting to be promoted)")
        return 0

    print(f"\n  FLOW BROKEN — {len(bypassed)} commit(s) on {production} that never "
          f"passed through {integration}:")
    for line in bypassed[:8]:
        print(f"    {line[:78]}")
    print(f"\n  The rule is: feature branch -> {integration} -> {production}.")
    print(f"  Anything on {production} that {integration} does not have skipped a step,")
    print(f"  even if it was 'only' config or docs.")

    if "--fix" in sys.argv:
        # Run from anywhere else, `git merge <production>` merged production into
        # itself, "Already up to date", and this printed "fixed" (2026-09-19).
        cur = sh("rev-parse", "--abbrev-ref", "HEAD")
        if cur != integration:
            print(f"\n  --fix must run from the {integration} worktree (this one is on "
                  f"{cur}) — nothing changed.")
            return 1
        # Merge, never reset: development may carry work of its own that is not
        # yet promoted, and a reset would delete it.
        r = subprocess.run([GIT, "-c", "user.name=cto", "-c", "user.email=cto@local",
                            "merge", "--no-edit", production],
                           cwd=REPO, capture_output=True, text=True)
        if r.returncode == 0:
            print(f"\n  fixed: merged {production} into {integration}")
        else:
            print(f"\n  could not merge automatically:\n    {r.stderr.strip()[:160]}")
            return 1
        return 0

    print(f"\n  To fix, from the {integration} worktree:  git merge {production}")
    print(f"  Then keep it that way: land work on {integration} first.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
