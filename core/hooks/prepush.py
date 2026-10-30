#!/usr/bin/env python3
"""Refuse to push unreviewed product code - or anything that skipped the flow -
onto a protected branch. Judged by the refs git is ACTUALLY pushing.

    python3 .ai-teammates/hooks/prepush.py          # check this branch, exit 1 if it would block
    python3 .ai-teammates/hooks/prepush.py --push   # check, then push
    (installed as the git pre-push hook)            # stdin: <lref> <lsha> <rref> <rsha>

Override - announced and logged, reachable from any harness because it is the
environment of git itself, not of the agent runtime:

    AI_TEAMMATES_ALLOW='<why>' git push origin main

WHY IT IS A GIT HOOK (2026-09-19). The previous form was called by guard.py,
which pattern-matched the command and then asked this script about the CURRENT
branch. So `git push origin feat:main` from a feature branch passed - the
refspec bypass the 2026-08-23 audit found, reopened - and `git push origin HEAD`
or `git -C . push origin main` never matched at all. Only a pre-push hook is
told by git where each ref will land, whatever spelling the command used, and
it fires in every harness, script and terminal. It lives in the common git dir,
so every worktree shares one copy.

Also fixed: with no upstream this scanned the whole history, so the first push
of a new branch (e.g. development) was blocked by years-old commits on main.
"New" now means: not already on any remote.

A push that lands on production also has to clear the independent-review gate
(review.py) - see review_gate().
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _config  # noqa: E402

GIT = _config.GIT
REPO = _config.repo_root()
ZERO = "0" * 40


def sh(*args):
    return subprocess.run([GIT, *args], cwd=REPO, capture_output=True,
                          text=True).stdout.strip()


def new_commits(local, remote, *exclude):
    """rev-list args for commits the remote does not have yet."""
    if remote and remote != ZERO:
        return [local, "^" + remote, *("^" + x for x in exclude)]
    return [local, *("^" + x for x in exclude), "--not", "--remotes"]


def review_gate(local, remote):
    """Independent review for a push onto production: the check guard.py runs on a
    typed `git merge development`, at the door guard.py cannot see.

    FOUND 2026-09-30: guard.py gates a SPELLING - `git merge <integration>` on the
    production checkout. `git push origin development:main` promotes the same
    commits and never met it. Measured: an unreviewed src/auth.ts merged into
    development went to main, exit 0. This hook is told where a ref lands
    whatever the spelling, so the same check runs here, over exactly the commits
    being pushed.

    FAIL CLOSED: if the check cannot run - review.py missing, a remote tip we do
    not have, git unreadable - the push is blocked and says so. A gate that
    raises and lets the push through is off exactly when something is wrong.

    A first push of production to a remote that has none (remote sha all zeros)
    is judged like any other: over the commits not on another remote, which is
    all of history when nothing else is remote. Deliberate (2026-09-30). Skipping
    it would be a way round the gate for whoever can make the ref look absent,
    and the product-code rule above already treats those same commits as new. A
    project that ran under the guardrails passes on its filed reviews; one
    written before them cannot be reviewed retroactively and announces the push
    once, which is what an override is for."""
    first = not remote or remote == ZERO
    label = local[:12] if first else f"{remote[:12]}..{local[:12]}"
    try:
        import review
        pending = review.unreviewed(new_commits(local, remote))
        if not pending:
            return []
        text = review.report(pending, label)
    except Exception as e:
        return [f"independent-review gate COULD NOT RUN ({type(e).__name__}: {str(e)[:140]}) - "
                f"blocked, not waved through: a promotion nobody could check is not a reviewed one"]
    if first:
        text += ("\n\nNo production branch on this remote yet, so every commit not already on another "
                 "remote counts as new.\nAn initial push is announced, not reviewed: "
                 "AI_TEAMMATES_ALLOW='<why>'.")
    return [text]


def violations(local, remote, dest):
    """Why pushing `local` onto branch `dest` is not allowed. [] = allowed."""
    if dest not in _config.protected():
        return []
    production, integration = _config.branches()
    # First: the override log keeps only the first problems, and this is the one
    # that must not fall off the end of it.
    out = review_gate(local, remote) if dest == production and integration else []

    # Only commits typed on this branch's own line count: files that arrived by
    # merging a reviewed branch are what development exists to accumulate.
    direct = sh("log", "--first-parent", "--no-merges", "--name-only",
                "--pretty=format:", *new_commits(local, remote)).splitlines()
    guarded = sorted({f for f in direct if f and f.startswith(_config.guarded())})
    out += [f"product file committed directly to {dest}: {f}" for f in guarded[:8]]

    # DIRECTION, not file type: production advances only by what the
    # integration branch already has. A property of FILES cannot enforce this.
    if dest == production and integration and sh("rev-parse", "--verify", "--quiet", integration):
        skipped = sh("log", "--no-merges", "--format=%h %s",
                     *new_commits(local, remote, integration)).splitlines()
        out += [f"not on {integration} first: {l[:70]}" for l in skipped[:8] if l]
    return out


def decide(problems):
    if not problems:
        return 0
    print("\n  BLOCKED — this push would put unreviewed work on a protected branch:")
    for p in problems:
        print(textwrap.indent(p, "    "))          # a problem may be a paragraph (the review report)
    reason = os.environ.get("AI_TEAMMATES_ALLOW") or ("--force-allow" if "--force-allow" in sys.argv else "")
    if reason:
        try:
            from guard import log
            log("override", {"rule": "prepush", "reason": reason[:200], "problems": problems[:8]})
        except Exception:
            pass
        print(f"\n  OVERRIDDEN ({reason}). Say so in your report, with the files and the reason.")
        return 0
    print("\n  Product code goes through a feature branch; production only via the integration branch.")
    print("  Genuine hotfix? Say so out loud, then: AI_TEAMMATES_ALLOW='<why>' git push ...")
    return 1


def run_hook():
    problems = []
    for line in sys.stdin.read().splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        _, local, rref, remote = parts
        dest = rref[len("refs/heads/"):] if rref.startswith("refs/heads/") else rref
        if local == ZERO:
            if dest in _config.protected():
                problems.append(f"deleting protected branch {dest}")
            continue
        problems += violations(local, remote, dest)
    return decide(problems)


def run_manual():
    branch = sh("rev-parse", "--abbrev-ref", "HEAD")
    upstream = sh("rev-parse", "--abbrev-ref", "@{u}")
    dest = upstream.split("/", 1)[1] if "/" in upstream else branch
    remote = sh("rev-parse", "--verify", "--quiet", "@{u}") if upstream else ""
    rc = decide(violations(sh("rev-parse", "HEAD"), remote, dest))
    if rc == 0:
        print(f"  OK — {branch} -> {dest}")
    if rc == 0 and "--push" in sys.argv:
        env = dict(os.environ)
        if "--force-allow" in sys.argv:
            env.setdefault("AI_TEAMMATES_ALLOW", "--force-allow")
        out = subprocess.run([GIT, "push", "origin", branch], cwd=REPO, env=env,
                             capture_output=True, text=True)
        print(f"  push: {'ok' if out.returncode == 0 else out.stderr.strip()[-300:]}")
        return out.returncode
    return rc


if __name__ == "__main__":
    sys.exit(run_hook() if "--hook" in sys.argv else run_manual())
