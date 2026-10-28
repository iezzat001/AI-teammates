#!/usr/bin/env python3
"""Don't let a session's thinking die in the working tree.

WHY THIS EXISTS (2026-09-10). The CTO spent a session decomposing a ranking
signal, wrote a precise result to board.json — variance shares, Spearman
coefficients, the decisive outlier — and then the session ended. The finding
existed only as an uncommitted file. One crash and the analysis was gone, and
this project has been OOM-killed three times.

The measurement is the expensive part. Committing it is free. Anything free that
protects something expensive should not depend on someone remembering.

So: at session end, commit the RECORD automatically — board, docs, facts. Never
product code: that still goes through a feature branch and prepush, and an
auto-commit of src/ would be exactly the unreviewed landing those rules exist to
stop. Product dirt is reported loudly instead, because leaving it silent is how
work gets lost the other way.

    python3 session_end.py           # commit the record, report the rest
    python3 session_end.py --check   # report only, change nothing
"""
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _config  # noqa: E402

GIT = _config.GIT
REPO = _config.repo_root()

# Safe to commit unattended: the session's record of what it learned and decided.
# Not docs/: those are vendored and install.sh overwrites them on upgrade, so
# committing edits there only schedules them for deletion. Project notes go in
# RULES.md or briefs/.
RECORD = [".ai-teammates/board.json", ".ai-teammates/facts.json",
          ".ai-teammates/RULES.md", ".ai-teammates/briefs/", ".ai-teammates/cards/"]


def sh(*args):
    return subprocess.run([GIT, *args], cwd=REPO,
                          capture_output=True, text=True).stdout.strip()


def main() -> int:
    check_only = "--check" in sys.argv

    dirty = [l for l in sh("status", "--porcelain").splitlines() if l]
    if not dirty:
        return 0

    # NOT l[3:]. git porcelain is "XY <path>", but sh() ends with .strip(),
    # which eats the leading space of the FIRST line only — so a fixed offset
    # silently ate one character of exactly one path per run. It turned
    # ".ai-teammates/board.json" into "ai-teammates/board.json", which matched
    # no RECORD prefix, so the one file this hook exists to protect was the one
    # file it classified as "other" and refused to commit. Found by testing the
    # hook against the very case it was written for.
    paths = []
    for l in dirty:
        m = re.match(r"^\s*[A-Z?!ADMRCU ]{1,2}\s+(.*)$", l)
        if m:
            paths.append(m.group(1).strip().strip('"'))
    record = [p for p in paths if any(p.startswith(r) for r in RECORD)]
    guarded = [p for p in paths if p.startswith(_config.guarded())]
    other = [p for p in paths if p not in record and p not in guarded]

    # FOUND 2026-09-19: this committed onto whatever was checked out - main, for
    # the CTO - so the next pre-flight flow.py failed on a commit this hook made.
    # It produced the `chore(session)` bypasses flow.py was written about.
    production, _ = _config.branches()
    on_production = sh("rev-parse", "--abbrev-ref", "HEAD") == production

    if record and on_production:
        print(f"  session: {len(record)} record file(s) uncommitted - not auto-committed "
              f"onto {production}; commit them on the integration branch: "
              f"{', '.join(record[:3])}")
    elif record and not check_only:
        sh("add", "--", *record)
        # Only commit if something actually staged — `git add` on an ignored or
        # already-clean path is a no-op and an empty commit would be noise.
        if sh("diff", "--cached", "--name-only", "--", *record):
            msg = ("chore(session): commit the session record\n\n"
                   "board/docs/facts written during the session, committed so the\n"
                   "measurement does not live only in the working tree. Product code\n"
                   "is deliberately NOT auto-committed - that goes through a feature\n"
                   "branch and prepush.")
            # The record paths, and nothing else that happens to be staged. FOUND 2026-10-28 (repo
            # analysis): a bare `git commit` took the whole index, so staged product code went into
            # this commit while the report below said it was "not auto-committed".
            r = subprocess.run([GIT, "-c", "user.name=cto",
                                "-c", "user.email=cto@local", "commit", "-q", "-m", msg, "--", *record],
                               cwd=REPO, capture_output=True, text=True)
            if r.returncode == 0:
                print(f"  session: committed the record ({len(record)} path(s))")
            else:
                print(f"  session: could not commit record — {r.stderr.strip()[:90]}")
    elif record:
        print(f"  session: {len(record)} record file(s) uncommitted: {', '.join(record[:3])}")

    if guarded:
        print(f"\n  UNCOMMITTED PRODUCT CODE — {len(guarded)} file(s), not auto-committed:")
        for p in guarded[:6]:
            print(f"    {p}")
        print("  These need a feature branch. They will not survive a crash as-is.")

    if other:
        print(f"  session: {len(other)} other uncommitted file(s): {', '.join(other[:4])}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
