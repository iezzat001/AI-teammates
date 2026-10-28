#!/usr/bin/env python3
"""Build a demo project, serve its dashboard, and optionally screenshot every tab.

    python3 scripts/dashboard_demo.py                     # serve http://127.0.0.1:8790 until Ctrl-C
    python3 scripts/dashboard_demo.py --shots docs/images # write dashboard-<tab>.png and exit

The demo is an invented app, "acme-notes", installed with this checkout's install.sh into
/tmp/ai-teammates-demo, with a board, branches and worktrees, a card run and the hook events a
real session leaves. Everything the dashboard shows comes from the real dashboard_server.py
reading that project, so the screenshots show what the dashboard shows. It runs with its own
HOME, so nothing of yours is read or written. Screenshots need Google Chrome or Chromium.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
ROOT = Path("/tmp/ai-teammates-demo")
PROJ = ROOT / "acme-notes"
DEV = ROOT / "acme-notes-dev"
CARD_WT = ROOT / "acme-notes-card-ui-7"
HOME = ROOT / "home"
PORT = int(os.environ.get("DEMO_PORT", 8790))   # not 8899: that is where your own project's dashboard runs
TABS = ["board", "git", "team", "rules", "activity"]
NOW = datetime.now(timezone.utc)


def git(*a, cwd=PROJ, when=None):
    env = {**os.environ, "HOME": str(HOME), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when.isoformat()
    r = subprocess.run(["git", *a], cwd=cwd, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"git {' '.join(a)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def commit(files: dict, msg: str, cwd=PROJ, hours_ago=0.0, author=None):
    for rel, text in files.items():
        p = cwd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git("add", "-A", cwd=cwd)
    extra = ["-c", f"user.name={author}", "-c", f"user.email={author}@seat.local"] if author else []
    git(*extra, "commit", "-qm", msg, cwd=cwd, when=NOW - timedelta(hours=hours_ago))


def iso(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def build():
    if ROOT.exists():
        # only ever remove what this script made
        if not (ROOT / ".ai-teammates-demo").exists():
            sys.exit(f"{ROOT} exists and was not made by this script; remove it yourself first")
        for wt in (DEV, CARD_WT):
            if wt.exists():
                subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=PROJ, capture_output=True)
        shutil.rmtree(ROOT)
    (HOME / "bin").mkdir(parents=True)
    (ROOT / ".ai-teammates-demo").write_text("made by scripts/dashboard_demo.py\n")
    # install.sh wants `claude` on PATH; the demo never runs a model
    stub = HOME / "bin" / "claude"
    stub.write_text("#!/bin/sh\n[ \"$1\" = auth ] && echo '{\"loggedIn\": true}'\nexit 0\n")
    stub.chmod(0o755)

    PROJ.mkdir()
    git("init", "-q", "-b", "main")
    git("config", "user.email", "you@example.com")
    git("config", "user.name", "You")
    commit({"README.md": "# Acme Notes\n", "package.json": '{"name": "acme-notes"}\n',
            "src/api/notes.ts": "export const list = () => []\n", "src/components/NotesList.tsx": "export {}\n"},
           "initial app", hours_ago=240)
    commit({"src/api/share.ts": "export const share = () => null\n"}, "feat: share links", hours_ago=170)
    commit({"src/api/notes.ts": "export const list = (page = 1) => []\n"}, "API-2: paginate GET /notes",
           hours_ago=150, author="seat:backend")

    env = {**os.environ, "HOME": str(HOME), "PATH": f"{HOME / 'bin'}:{os.environ['PATH']}",
           "GIT_CONFIG_GLOBAL": os.devnull}
    r = subprocess.run([str(SRC / "install.sh")], cwd=PROJ, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("install.sh failed:\n" + r.stdout + r.stderr)
    ai = PROJ / ".ai-teammates"
    cfg = json.loads((ai / "config.json").read_text())
    cfg.update(owner="You", project="acme-notes")
    cfg["cto"].update(model="claude-opus-5-5", effort="max")       # the setup docs/04-ROUTING recommends
    (ai / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")
    (ai / "RULES.md").write_text(
        "Deploys\n"
        "  - Production deploys need the owner's word, in the session, every time.\n"
        "  - Staging deploys run `npm run deploy:staging` from the development checkout only.\n"
        "Data\n"
        "  - Never run a migration without a fresh backup: `npm run db:backup` first.\n"
        "  - The share-link tokens are user data: never log them, never print them.\n")
    git("add", "-A")
    git("commit", "-qm", "chore: install AI-teammates", when=NOW - timedelta(hours=140))

    git("branch", "development")
    git("worktree", "add", "-q", str(DEV), "development")
    commit({"src/components/NoteCard.tsx": "export {}\n"}, "UI-6: note titles no longer truncate mid-word",
           cwd=DEV, hours_ago=30, author="seat:ui-developer")
    commit({"src/api/share.ts": "export const share = (id) => null // expires\n"},
           "feat: share links expire after 7 days", cwd=DEV, hours_ago=20)
    commit({"README.md": "# Acme Notes\n\nnpm install && npm run dev\n"}, "DOC-1: setup steps match the scripts",
           cwd=DEV, hours_ago=6)
    git("worktree", "add", "-q", "-b", "feat/ui-ui-7", str(CARD_WT), "development")
    commit({"src/components/NotesList.tsx": "export {} // empty state\n",
            "src/components/EmptyState.tsx": "export const EmptyState = () => null\n"},
           "UI-7: empty state for the notes list", cwd=CARD_WT, hours_ago=0.6, author="seat:ui-developer")
    git("branch", "feat/db-api-3", "development")
    git("branch", "feature/search", "main")

    ui7 = git("rev-parse", "--short", "feat/ui-ui-7")
    tasks = [
        ("UI-8", "Dark-mode tokens on the settings page", "ui-developer", "todo",
         "tier mechanical · 3 files in scope · acceptance: typecheck, and grep for the 4 token names"),
        ("SEC-2", "Audit share links before the promotion", "security-audit", "todo",
         "review.py: 2 security-touching commits in main..development (src/api/share.ts); "
         "promotion is blocked until a read-only audit covers them"),
        ("API-4", "Retry transient 503s from the sync API", "backend", "todo",
         "BLOCKED: auth | SYNC_API_KEY is missing from .env in the card worktree (only the owner can add it)"),
        ("API-3", "Rate-limit POST /share", "backend", "doing",
         f"run {iso(4)[:16]}Z: started · budget 15 min / 40 tool calls · branch feat/db-api-3"),
        ("UI-7", "Empty state for the notes list", "ui-developer", "review",
         f"run {iso(36)[:16]}Z: done · 2 file(s) 2 files changed, 2 insertions(+), 1 deletion(-) · "
         f"acceptance 3/3 · 41s · 23 tool calls · $0.031 · feat/ui-ui-7 @ {ui7}"),
        ("OPS-4", "Deploy development to staging", "cto", "review",
         "after UI-7 merges · check_build_env.py on the built bundle first (build-time config)"),
        ("DOC-1", "README setup steps match the scripts", "cto", "done",
         "measured: 3 of 7 documented commands no longer existed; fixed and re-run end to end"),
        ("UI-6", "Note titles truncate mid-word", "ui-developer", "done",
         "blocked once (scope: NoteCard.tsx was not on the card), re-run with it added: done in 28s, acceptance 2/2"),
        ("API-2", "Paginate GET /notes", "backend", "done",
         "done · 3 files · acceptance 4/4 (row counts 50 / 50 / 12 on the seed data) · merged"),
    ]
    board = {"updated": NOW.date().isoformat(), "columns": ["todo", "doing", "review", "done"], "agents": {},
             "tasks": [{"id": i, "title": t, "agent": a, "status": s, "updated": NOW.date().isoformat(), "note": n}
                       for i, t, a, s, n in tasks]}
    for wt in (PROJ, DEV):
        (wt / ".ai-teammates" / "board.json").write_text(json.dumps(board, indent=2) + "\n")
    git("add", "-A", cwd=DEV)
    git("commit", "-qm", "board: UI-7 to review", cwd=DEV, when=NOW - timedelta(minutes=30))

    state = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir")) / "ai-teammates"
    state.mkdir(exist_ok=True)
    ev = lambda m, agent, kind="tool", **kw: {"ts": iso(m), "kind": kind, "agent": agent, **kw}
    events = [
        ev(44, "cto", tool="Bash", target="python3 .ai-teammates/hooks/card.py check UI-7", ok=True),
        ev(43, "cto", tool="Bash", target="python3 .ai-teammates/hooks/seat.py run --card UI-7", ok=True),
        ev(42, "ui-developer", tool="Read", target="src/components/NotesList.tsx", ok=True),
        ev(41, "ui-developer", tool="Edit", target="src/components/NotesList.tsx", ok=True),
        ev(41, "ui-developer", "blocked", rule="card-scope",
           message="src/styles/theme.css is outside this card's scope. You may change only: "
                   "src/components/NotesList.tsx, src/components/EmptyState.tsx."),
        ev(40, "ui-developer", tool="Write", target="src/components/EmptyState.tsx", ok=True),
        ev(39, "ui-developer", tool="Bash", target="python3 .ai-teammates/hooks/card.py accept", ok=True),
        ev(35, "cto", tool="Bash", target="git diff --stat development..feat/ui-ui-7", ok=True),
        ev(33, "cto", "blocked", rule="flow-direction",
           message="This commits directly onto main. The rule is feature -> development -> main."),
        ev(32, "cto", tool="Bash", target="git commit -m 'board: UI-7 to review'", ok=True),
        ev(9, "security-audit", tool="Grep", target="Authorization", ok=True),
        ev(8, "security-audit", tool="Read", target="src/api/share.ts", ok=True),
        ev(5, "backend", tool="Read", target="src/api/share.ts", ok=True),
        ev(4, "backend", tool="Edit", target="src/api/share.ts", ok=True),
        ev(3, "backend", "blocked", rule="seat-deny",
           message="A seat never runs 'git push*' (config.json required_denies)."),
        ev(2, "cto", "override", rule="prepush",
           reason="hotfix: share links 500 in production, owner approved in session"),
        ev(1, "backend", tool="Bash", target="npm test -- share", ok=True),
    ]
    (state / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    runs = [{"ts": iso(44), "agent": "ui-developer", "seat": "UI Developer", "exit": 0, "tool_calls": 23,
             "card": "UI-7", "status": "done", "seconds": 41, "cost_usd": 0.031},
            {"ts": iso(9000), "agent": "backend", "seat": "Backend", "exit": 0, "tool_calls": 18, "card": "API-2",
             "status": "done", "seconds": 64, "cost_usd": 0.044},
            {"ts": iso(12), "agent": "security-audit", "seat": "Security Audit", "exit": 0, "tool_calls": 31,
             "status": "done", "seconds": 212}]
    (state / "agent_runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in runs))

    # a claude-mem database that has kept this project's last session, so the memory chip is honest
    mem = HOME / ".claude-mem"
    (mem / "logs").mkdir(parents=True)
    (mem / "settings.json").write_text(json.dumps({"CLAUDE_MEM_DATA_DIR": str(mem)}))
    con = sqlite3.connect(mem / "claude-mem.db")
    con.executescript("create table observations(project text, created_at_epoch int);"
                      "create table session_summaries(project text, created_at_epoch int);"
                      "create table sdk_sessions(project text, started_at_epoch int);")
    ms = int((NOW - timedelta(minutes=30)).timestamp() * 1000)
    con.execute("insert into observations values ('acme-notes-dev', ?)", (ms,))
    con.execute("insert into sdk_sessions values ('acme-notes-dev', ?)", (ms,))
    con.commit()
    con.close()


def serve():
    env = {**os.environ, "HOME": str(HOME), "GIT_CONFIG_GLOBAL": os.devnull}
    return subprocess.Popen([sys.executable, str(DEV / ".ai-teammates" / "hooks" / "dashboard_server.py"), str(PORT)],
                            cwd=DEV, env=env)


def chrome() -> str:
    for c in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              "/Applications/Chromium.app/Contents/MacOS/Chromium",
              shutil.which("google-chrome") or "", shutil.which("chromium") or "", shutil.which("chromium-browser") or ""):
        if c and Path(c).exists():
            return c
    sys.exit("screenshots need Google Chrome or Chromium")


def shots(out: Path):
    out.mkdir(parents=True, exist_ok=True)
    browser = chrome()
    # (width, height) per tab: the content's measured size at that width plus a margin, so an image
    # carries no empty page. Re-measure if the demo data or the page layout changes.
    sizes = {"board": (1360, 695), "git": (1040, 535), "team": (1040, 520), "rules": (1040, 790),
             "activity": (1040, 722)}
    for tab in TABS:
        f = out / f"dashboard-{tab}.png"
        prof = ROOT / "chrome-profile"
        subprocess.run([browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                        f"--user-data-dir={prof}", "--force-device-scale-factor=1.5",
                        "--window-size=%d,%d" % sizes[tab], "--virtual-time-budget=6000",
                        f"--screenshot={f}", f"http://127.0.0.1:{PORT}/#{tab}"],
                       capture_output=True, text=True, timeout=120)
        print(f"  {'wrote' if f.exists() else 'FAILED'} {f}")


def main():
    build()
    proc = serve()
    time.sleep(1.5)
    try:
        if "--shots" in sys.argv:
            shots(Path(sys.argv[sys.argv.index("--shots") + 1]).resolve())
        else:
            print(f"  demo project: {DEV}\n  dashboard:    http://127.0.0.1:{PORT}   (Ctrl-C to stop)")
            proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
