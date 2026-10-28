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
"""
import os
import subprocess
import sys
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


def violations(local, remote, dest):
    """Why pushing `local` onto branch `dest` is not allowed. [] = allowed."""
    if dest not in _config.protected():
        return []
    # Only commits typed on this branch's own line count: files that arrived by
    # merging a reviewed branch are what development exists to accumulate.
    direct = sh("log", "--first-parent", "--no-merges", "--name-only",
                "--pretty=format:", *new_commits(local, remote)).splitlines()
    guarded = sorted({f for f in direct if f and f.startswith(_config.guarded())})
    out = [f"product file committed directly to {dest}: {f}" for f in guarded[:8]]

    # DIRECTION, not file type: production advances only by what the
    # integration branch already has. A property of FILES cannot enforce this.
    production, integration = _config.branches()
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
        print(f"    {p}")
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
