#!/usr/bin/env python3
"""Security-touching changes need a second pair of eyes before they reach
production. Not the same pair that wrote them.

WHY THIS EXISTS (2026-09-10). The CTO wrote authentication in front of a proxy
that spends money and writes user data, reviewed its own work, merged it, and
deployed it. The security-audit seat existed - Claude Code in plan mode,
read-only, on the model that has found three real bypasses in three runs - and
was never launched. Across a whole day of work, every closed card read
`agent=cto` and not one seat ran.

That was not defiance. The role file said "delegate bounded work" and never said
when delegation is REQUIRED, so it was advice, and advice loses to "I can just
do this". Same shape as the branch flow: a stated rule with nothing checking it.

So this states it as a condition a machine can evaluate: if the diff being
promoted touches auth, secrets, permissions or money, an audit record must exist
that covers those commits, and it must not have been produced by the author.

Two doors lead to production and both ask this file: guard.py, on a typed
`git merge development`, and prepush.py, on every push that lands on production
however it is spelled. Until 2026-09-30 only the first did.

    python3 review.py                      # check what development would promote
    python3 review.py --range main..HEAD   # check a specific range
    python3 review.py --record <agent>     # file an audit record for the current HEAD
"""
import json
import re
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _config  # noqa: E402

GIT = _config.GIT
REPO = _config.repo_root()
REVIEWS = _config.state_dir() / "reviews.jsonl"

# Paths and content that decide who can do what, or what gets spent.
#
# PRECISION MATTERS MORE THAN RECALL HERE. The first version matched "auth"
# anywhere, so it flagged every file under a `_authenticated/` route group, and
# matched bare "deny"/"allow"/"permission" in content, so it flagged the
# guardrail scripts themselves. A check that fires on ordinary work is the
# "rule nobody can satisfy" failure - people override it by habit and then miss
# the real one. Narrow enough to be believed.
SENSITIVE_PATH = re.compile(
    r"(?:^|/)("
    r"[^/]*auth[^/]*\.(ts|tsx|js|py|go|rb)|"     # authX.ts, not _authenticated/
    r"[^/]*(token|secret|credential|keyring|allowlist)[^/]*|"
    r"\.env[^/]*|wrangler\.[^/]+|"
    r"[^/]*(middleware|guard|policy|rbac|acl)[^/]*\.(ts|tsx|js|py)"
    r")$", re.I)
# Content signals that actually move a trust boundary. No bare deny/allow/verify.
SENSITIVE_CODE = re.compile(
    r"(Authorization\s*[:=]|Bearer\s|service_role|"
    r"api[_-]?key\s*[:=]|SECRET[_A-Z]*\s*[:=]|"
    r"\.signIn|\.signUp|getUser\(|getSession\(|"
    r"jwt|hmac|createHmac|signature|"
    r"USER_IDS|_ALLOWLIST)", re.I)
# Never flag the guardrails themselves — they are full of these words by nature.
EXEMPT_PATH = re.compile(r"\.ai-teammates/|(^|/)docs/|\.md$", re.I)


def sh(*args, check=False):
    r = subprocess.run([GIT, *args], cwd=REPO, capture_output=True, text=True,
                       errors="replace")     # a Latin-1 subject must not crash the gate
    if check and r.returncode != 0:
        # An empty answer reads as "nothing security-touching"; a git failure must not.
        raise RuntimeError(f"git {args[0]} failed: {(r.stderr.strip().splitlines() or ['?'])[0][:160]}")
    return r.stdout.strip()


def _adds_sensitive_code(sha: str) -> bool:
    """Does the diff of `sha` ALONE add a SENSITIVE_CODE line to a file that is
    not exempt?

    One `git show` per commit, read as a stream and attributed to files by its
    own headers. The command line never carries a file list (ARG_MAX on a huge
    commit) and a huge diff costs time, not memory. -U0: added lines only. A
    merge shows its combined diff, one column per parent; only a line added
    against EVERY parent is new in the merge itself - the rest came from a
    branch whose own commits are judged in their own turn."""
    cmd = [GIT, "show", "--format=", "-U0", "--no-color",
           "--src-prefix=a/", "--dst-prefix=b/", sha]   # pinned: diff.noprefix must not shift the paths
    with subprocess.Popen(cmd, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, errors="replace") as p:
        scan, cols = False, 0    # scan: this file is not exempt; cols: >0 inside a hunk
        for line in p.stdout:
            line = line.rstrip("\n")
            if line.startswith("diff "):            # next file: back in its header
                scan, cols = False, 0
            elif not cols and line.startswith("+++ "):
                name = line[4:].rstrip("\t").strip('"')   # git tabs-terminates names with spaces
                name = name[2:] if name.startswith("b/") else name
                scan = name != "/dev/null" and not EXEMPT_PATH.search(name)
            elif line.startswith("@@"):
                cols = line.index(" ") - 1          # "@@ " = 1 column, "@@@ " = 2 (a merge)
            elif cols and scan and line[:cols] == "+" * cols and SENSITIVE_CODE.search(line[cols:]):
                return True
        err = p.stderr.read()
        if p.wait() != 0:                           # half a diff is not "nothing found"
            raise RuntimeError(f"git show {sha[:7]} failed: {(err.strip().splitlines() or ['?'])[0][:160]}")
    return False


def load_reviews() -> list:
    if not REVIEWS.exists():
        return []
    out = []
    for line in REVIEWS.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _completed_audit_run(agent: str, rng: str) -> bool:
    """A finished, exit-0 run of a READ-ONLY seat, started after the newest
    commit it claims to cover.

    FOUND 2026-09-19: `--record cto` passed the gate. Nothing compared the
    reviewer to anything, so the author could certify its own work - the one
    thing this file exists to prevent. A name is not evidence; a run is."""
    import seat
    a = seat.load_agents().get(agent)
    if not a or a.get("read_only") != "true":
        return False
    newest = sh("log", "-1", "--format=%cI", rng)
    if not newest or not seat.RUNS.exists():
        return False
    since = datetime.fromisoformat(newest)
    for line in seat.RUNS.read_text().splitlines():
        try:
            r = json.loads(line)
            # A card run can exit 0 and still be blocked or failed: it must have ended done.
            if (r.get("agent") == agent and r.get("exit") == 0
                    and r.get("status", "done") == "done"
                    and datetime.fromisoformat(r["ts"]) >= since):
                return True
        except Exception:
            continue
    return False


def record(agent: str, rng: str) -> int:
    if not _completed_audit_run(agent, rng):
        print(f"  refused: no completed run of a read-only seat '{agent}' after the "
              f"newest commit in {rng}.\n  Reviews come from an audit run, not from "
              f"the author. Run:\n    python3 .ai-teammates/hooks/seat.py run "
              f"security-audit \"Audit {rng}. Report findings only.\"")
        return 1
    commits = [l for l in sh("log", "--format=%H", rng).splitlines() if l]
    REVIEWS.parent.mkdir(parents=True, exist_ok=True)
    with REVIEWS.open("a") as f:
        f.write(json.dumps({
            "at": datetime.now(timezone.utc).isoformat(),
            "reviewer": agent,
            "range": rng,
            "commits": commits,
        }) + "\n")
    print(f"  review filed by {agent} covering {len(commits)} commit(s)")
    return 0


def scan(revs):
    """(how many commits `revs` selects, which of them touch access or spend).

    `revs` is whatever `git log` takes: ["main..development"] here, prepush's
    [local, "^remote"] there. The second value is [(sha7, subject, hits)]."""
    commits = [l for l in sh("log", "--format=%H %s", *revs, check=True).splitlines() if l]
    flagged = []
    for line in commits:
        sha, _, subj = line.partition(" ")
        files = [f for f in sh("show", "--name-only", "--format=", sha, check=True).splitlines()
                 if f and not EXEMPT_PATH.search(f)]
        hits = [f for f in files if SENSITIVE_PATH.search(f)]
        # Path names are not the whole story - a plain-looking file can add an
        # Authorization header. Look at what THIS commit adds, in files we did
        # not exempt.
        #
        # FOUND 2026-09-30: this ran `git show -- <files>` with no sha, so it read
        # HEAD's diff of those paths, not this commit's. `Authorization: Bearer`
        # added in A read "none security-touching" as soon as any later commit
        # touched the same file - a check pointed at a source that cannot see the
        # thing (catalogue #1). When that came back empty it fell back to the WHOLE
        # commit, exempt docs included, so a doc quoting "Authorization: Bearer"
        # flagged an unrelated commit.
        if not hits and files and _adds_sensitive_code(sha):
            hits = ["(content)"]
        if hits:
            flagged.append((sha[:7], subj[:56], hits[:3]))
    return len(commits), flagged


def uncovered(flagged):
    """Of `flagged`, the commits no filed review covers."""
    reviewed = set()
    for r in load_reviews():
        reviewed.update(c[:7] for c in r.get("commits", []))
    return [f for f in flagged if f[0] not in reviewed]


def unreviewed(revs):
    """[(sha7, subject, hits)] - the security-touching commits `revs` selects that
    no filed review covers. [] means clear to promote.

    This CLI (which guard.py runs on a typed `git merge development`) and
    prepush.py (every push onto production, however spelled) both answer through
    this, so the two doors to production cannot disagree about what needs a
    second pair of eyes. Raises if it cannot tell - callers fail closed."""
    return uncovered(scan(revs)[1])


def report(pending, rng):
    """The words for an unreviewed promotion: what, why, and the two commands that
    clear it. One text for both doors. `rng` is what the audit is asked to read
    and what --record files against."""
    lines = [f"UNREVIEWED SECURITY CHANGES — {len(pending)} commit(s) decide access "
             f"or spend and no independent review covers them:"]
    for sha, subj, hits in pending[:8]:
        lines += [f"  {sha}  {subj}", f"           touches: {', '.join(hits)}"]
    lines += ["", "These were written and are about to be promoted by the same agent.",
              "The security-audit seat is read-only by construction and exists for this.",
              "", "  python3 .ai-teammates/hooks/seat.py run security-audit \\",
              f"    \"Audit {rng}. Report findings only; you cannot write.\"",
              "", "Then file the result:",
              f"  python3 .ai-teammates/hooks/review.py --record security-audit --range {rng}"]
    return "\n".join(lines)


def main() -> int:
    production, integration = _config.branches()

    if "--record" in sys.argv:
        i = sys.argv.index("--record")
        agent = sys.argv[i + 1] if len(sys.argv) > i + 1 else "unknown"
        rng = production + "..HEAD"
        if "--range" in sys.argv:
            rng = sys.argv[sys.argv.index("--range") + 1]
        return record(agent, rng)

    if "--range" in sys.argv:
        rng = sys.argv[sys.argv.index("--range") + 1]
    elif integration:
        rng = f"{production}..{integration}"
    else:
        rng = "HEAD~1..HEAD"

    try:
        n, flagged = scan([rng])
    except RuntimeError as e:      # an unreadable range is not an empty one
        print(f"  review  ERROR cannot read {rng}: {e}")
        return 1
    if not n:
        print("  review  ok    nothing waiting to be promoted")
        return 0
    if not flagged:
        print(f"  review  ok    {n} commit(s) to promote, none security-touching")
        return 0

    pending = uncovered(flagged)
    if not pending:
        print(f"  review  ok    {len(flagged)} security-touching commit(s), all reviewed")
        return 0

    print("\n" + textwrap.indent(report(pending, rng), "  "))
    return 1


if __name__ == "__main__":
    sys.exit(main())
