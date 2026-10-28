#!/usr/bin/env python3
"""Shared config loader for the hooks.

Every hook used to carry its own hardcoded branch names and path prefixes. That
was fine for one project and is wrong for a portable system - and it also let
two hooks disagree: `guard.py` guarded `src/` while `prepush.py` guarded `src/`
AND `vps-backend/`, so backend code could be edited from the main checkout and
only caught later at push time, if at all.

One file, read by all of them. If config.json is missing or unreadable, fall
back to conservative defaults rather than to no protection - a guardrail that
silently disables itself on a bad config is the worst outcome available.
"""
import json
import os
import subprocess
from pathlib import Path

GIT = "/usr/bin/git" if os.path.exists("/usr/bin/git") else "git"

DEFAULTS = {
    # [production, integration]. "master" used to sit second here, which made it
    # the integration branch whenever config.json was missing.
    "protected_branches": ["main", "development"],
    "guarded_paths": ["src/", "lib/", "app/"],
    "required_denies": ["git push*", "rm -rf*", "ssh*"],
    # regexes for mechanical files inside guarded paths (generated code, tool config)
    "edit_exempt": [r"\.gen\.", r"\.config\."],
}


def repo_root() -> Path:
    """The worktree we are actually in — not the file's parent.

    A hook resolving its repo from __file__ reports the wrong root when it runs
    from a worktree, which is exactly when it matters most.
    """
    try:
        out = subprocess.run([GIT, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True).stdout.strip()
        if out:
            return Path(out)
    except Exception:
        pass
    return Path(__file__).resolve().parents[2]


def load() -> dict:
    cfg = dict(DEFAULTS)
    path = repo_root() / ".ai-teammates" / "config.json"
    if path.exists():
        try:
            cfg.update({k: v for k, v in json.loads(path.read_text()).items()
                        if v not in (None, [], "")})
        except Exception as e:
            # Loud, and keep the defaults. Never fall through to no protection.
            print(f"  WARNING: {path} unreadable ({e}) — using safe defaults")
    return cfg


def protected() -> tuple:
    return tuple(load()["protected_branches"])


def guarded() -> tuple:
    return tuple(load()["guarded_paths"])


def required_denies() -> list:
    return list(load()["required_denies"])


def branches() -> tuple:
    """(production, integration). One definition - guard, flow, review, prepush
    and session_end each used to derive these on their own, and disagreed."""
    cfg = load()
    prot = cfg["protected_branches"]
    production = cfg.get("production_branch") or prot[0]
    integration = cfg.get("integration_branch") or (prot[1] if len(prot) > 1 else None)
    return production, (integration if integration != production else None)


PREPUSH_MARK = "AI-teammates pre-push"


def common_dir() -> Path:
    return Path(subprocess.run([GIT, "rev-parse", "--path-format=absolute",
                                "--git-common-dir"], cwd=repo_root(),
                               capture_output=True, text=True).stdout.strip())


def state_dir() -> Path:
    """Runtime state - events, runs, reviews, seat logs - in the COMMON git dir.

    FOUND 2026-09-19: it lived in each worktree's .ai-teammates/. seat.py read
    main's events.jsonl while a seat's hooks wrote to the seat worktree's, so
    the audit seat always reported "0 tools, clean"; a review filed in one
    worktree was invisible to the guard in another; and none of it was
    gitignored, so every tree was permanently dirty and "dirty" meant nothing.
    One place, shared by every worktree, never committed."""
    d = common_dir() / "ai-teammates"
    d.mkdir(parents=True, exist_ok=True)
    return d


def hooks_dir() -> Path:
    """Where git looks for hooks: core.hooksPath if set, else the COMMON git dir,
    which every worktree shares - so one copy guards them all."""
    run = lambda *a: subprocess.run([GIT, *a], cwd=repo_root(),
                                    capture_output=True, text=True).stdout.strip()
    hp = run("config", "core.hooksPath")
    if hp:
        p = Path(hp).expanduser()
        return p if p.is_absolute() else repo_root() / p
    return common_dir() / "hooks"


def prepush_hook_installed() -> bool:
    f = hooks_dir() / "pre-push"
    try:
        return PREPUSH_MARK in f.read_text(errors="ignore")
    except OSError:
        return False

def mem_capture() -> dict:
    """Is claude-mem KEEPING sessions - not "does its worker answer".

    FOUND 2026-09-25: the worker answered /api/health for eight days while its
    observer could not sign in ("OAuth session expired and could not be
    refreshed"), so nothing was kept after 2026-09-17 while sessions went on.
    The dashboard chip and `cto` pre-flight both said OK: they asked the one
    component that was fine. The signal is sessions without captures.
    (Its pending queue is NOT the signal: 162 rows there date from June-July.)"""
    import re
    import sqlite3
    import time
    from datetime import datetime
    home = Path.home() / ".claude-mem"
    try:
        s = json.loads((home / "settings.json").read_text())
    except Exception:
        return {"ok": False, "days": None, "why": "claude-mem not installed (npx claude-mem install)"}
    db = Path(s.get("CLAUDE_MEM_DATA_DIR") or home) / "claude-mem.db"

    # FOUND 2026-10-02: the numbers below were a GLOBAL max, so any project's activity
    # (this very session's worktree) turned ContentOS green while it had captured nothing
    # since 09-10; and the worker's own failure to STORE (a missing column, 10,386 errors
    # since 08-03) was never consulted. So: this project's rows, and the worker's recent
    # persist errors. claude-mem names a project by folder, a worktree as "repo/worktree".
    names = {repo_root().name}
    try:
        main = common_dir().parent
        names |= {main.name, f"{main.name}/{repo_root().name}"}
    except Exception:
        pass
    marks = ",".join("?" for _ in names)
    like = " or ".join("project like ?" for _ in names)
    args = tuple(names) + tuple(f"{n}/%" for n in names)
    where = f"(project in ({marks}) or {like})"
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        one = lambda q: con.execute(q, args).fetchone()[0] or 0
        kept = max(one(f"select max(created_at_epoch) from observations where {where}"),
                   one(f"select max(created_at_epoch) from session_summaries where {where}"))
        session = one(f"select max(started_at_epoch) from sdk_sessions where {where}")
        store_ok = con.execute("select max(created_at_epoch) from observations").fetchone()[0] or 0   # any project
        con.close()
    except Exception as e:
        return {"ok": False, "days": None, "why": f"cannot read {db.name} ({type(e).__name__})"}

    # A worker that cannot store fails on EVERY tool call. So it is failing NOW when
    # its newest store error is newer than the newest thing it managed to store (any
    # project: that is about the worker, not the folder). Older errors are history, which
    # a repair leaves a day of: shown, not counted against it. A fixed time window was
    # tried first and kept reporting failure for half an hour after a verified repair.
    errors, latest = 0, 0.0
    for f in sorted((home / "logs").glob("claude-mem-*.log"))[-2:]:
        try:
            for line in f.read_text(errors="ignore").splitlines():
                # the LEVEL field, not "[ERROR]" anywhere in the line: claude-mem also logs the commands the
                # agent ran, so a debugging command that merely mentions these words matched itself
                if re.match(r"^\[[^\]]+\]\s+\[ERROR\]\s", line) and re.search(
                        r"no column named|no such column|SQLITE_", line):
                    try:
                        ts = datetime.strptime(line[1:20], "%Y-%m-%d %H:%M:%S").timestamp()
                    except ValueError:
                        continue
                    if ts >= time.time() - 86400:
                        errors += 1
                        latest = max(latest, ts)
        except OSError:
            pass
    sec = lambda ms: ms / 1000 if ms > 1e11 else ms
    failing_now = bool(latest) and latest > sec(store_ok)
    proj = "/".join(sorted(names, key=len)[-1:])
    ago = lambda t: f"{round((time.time() - t) / 3600, 1)}h ago"
    hist = f"; {errors} store errors in the last 24h, latest {ago(latest)}" if errors and not failing_now else ""
    if not kept:
        return {"ok": False, "days": None,
                "why": f"nothing kept yet for {proj}" + (f"; the worker is failing to STORE right now ({errors} errors in 24h)"
                                                          if failing_now else hist)}
    days = round((time.time() - sec(kept)) / 86400, 1)
    gap = (sec(session) - sec(kept)) / 86400 if session else 0
    ok = gap <= 1 and not failing_now    # sessions more than a day after the last capture = not kept
    why = f"last kept {days}d ago ({proj})"
    if gap > 1:
        why += (f", sessions went on for {round(gap, 1)}d after it"
                " - capture is failing (store errors, or the observer's sign-in: `claude auth login`)")
    if failing_now:
        why += f", and the worker is failing to STORE right now ({errors} errors in 24h, see ~/.claude-mem/logs)"
    else:
        why += hist
    return {"ok": ok, "days": days, "why": why}
