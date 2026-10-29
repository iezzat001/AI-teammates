#!/usr/bin/env python3
"""Serve the team dashboard, and compute git state live on request.

The old setup was `python3 -m http.server`, which can only return files that
already exist. Branch and worktree state written to a file goes stale the moment
anyone checks out anything - and a stale branch indicator is worse than none,
because it is believed. So /git.json is computed per request instead.

    python3 .ai-teammates/hooks/dashboard_server.py [port]

Endpoints: /git.json (live git state), /board.json (cards + generated roster),
/team.json, /rules.json, /health.json (all generated), /events.jsonl (activity
log from the shared state dir). Everything else is a static file from
config "dashboard_dir": by default the UI that ships with AI-teammates
(.ai-teammates/dashboard/index.html). Open http://127.0.0.1:8899 once it runs.
"""
import json
import subprocess
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _config  # noqa: E402

# The UI files may live elsewhere (config "dashboard_dir"); board and events
# always come from .ai-teammates. 2026-09-10: two board copies drifted and the
# Team tab showed seats retired in one but not the other, and the Activity tab
# served a frozen old events log. One canonical source each, served explicitly.
# Default: the UI that ships in .ai-teammates/dashboard. It used to be .ai-teammates itself, which also
# served config.json and facts.json to anything that could reach the port.
SERVE = REPO / _config.load().get("dashboard_dir", ".ai-teammates/dashboard")
PROD = _config.branches()[0]
# /board.json, /rules.json, /team.json and /health.json are generated (below).
DATA = {"/events.jsonl": (_config.state_dir() / "events.jsonl", "text/plain")}
# Apple's git in /usr/bin is 2.50; /usr/local/bin/git is a 2021 install (2.19)
# that lacks --show-current and --sparse. Pin the good one.
GIT = "/usr/bin/git" if Path("/usr/bin/git").exists() else "git"


def sh(*args, cwd=REPO):
    try:
        p = subprocess.run([GIT, *args], cwd=cwd, capture_output=True,
                           text=True, timeout=10)
        return p.stdout.strip() if p.returncode == 0 else ""
    except Exception:
        return ""


def git_state():
    current = sh("rev-parse", "--abbrev-ref", "HEAD")

    worktrees, cur = [], {}
    for line in sh("worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            if cur:
                worktrees.append(cur)
            cur = {"path": line[9:], "name": Path(line[9:]).name}
        elif line.startswith("branch "):
            cur["branch"] = line[7:].replace("refs/heads/", "")
        elif line.startswith("HEAD "):
            cur["head"] = line[5:][:7]
        elif line.startswith("detached"):
            cur["branch"] = "(detached)"
    if cur:
        worktrees.append(cur)

    checked_out = {w.get("branch") for w in worktrees}

    def entry(ref, name, remote):
        ahead = sh("rev-list", "--count", f"{PROD}..{ref}") or "0"
        behind = sh("rev-list", "--count", f"{ref}..{PROD}") or "0"
        return {
            "name": name,
            "ahead": int(ahead) if ahead.isdigit() else 0,
            "behind": int(behind) if behind.isdigit() else 0,
            "checkedOut": name in checked_out,
            "remote": remote,
            "last": sh("log", "-1", "--format=%h %s", ref)[:64],
        }

    local = [b for b in sh("for-each-ref", "--format=%(refname:short)",
                           "refs/heads/").splitlines() if b]
    branches = [entry(b, b, False) for b in local]
    # Branches that exist ONLY on a remote. Reading refs/heads alone made
    # unmerged work pushed from elsewhere (an audit report, a UI commit)
    # invisible here - a panel that cannot see the thing looks healthy.
    # Uses the last fetch; the panel does not fetch on its own.
    for r in sh("for-each-ref", "--format=%(refname:short)", "refs/remotes/").splitlines():
        remote_name, _, b = r.partition("/")
        if b and b != "HEAD" and r != remote_name and b not in local:
            branches.append(entry(r, r, True))
    branches.sort(key=lambda x: (-x["ahead"], x["name"]))

    dirty = [l for l in sh("status", "--porcelain").splitlines() if l.strip()]
    fh = _config.common_dir() / "FETCH_HEAD"          # when remote refs were last true
    return {
        "production": PROD,
        "current": current,
        "branches": branches,
        "worktrees": worktrees,
        "dirty": len(dirty),
        "dirtyFiles": [l[3:] for l in dirty[:8]],
        "fetchedAt": int(fh.stat().st_mtime) if fh.exists() else None,
        "counts": {"branches": len(local),
                   "remoteOnly": len(branches) - len(local),
                   "worktrees": len(worktrees)},
    }



# --- generated panels ---------------------------------------------------------
# 2026-09-25: the Team and Rules tabs read hand-kept files (board.json "agents",
# .claude/rules.json) and both drifted: a retired memory-curator seat, a model
# that never existed, a branch flow the hooks no longer enforce. They are now
# GENERATED from the sources the guardrails themselves read - the agent specs
# seat.py launches, config.json, and RULES.md - so they cannot disagree.
# Chosen to read on the dashboard's black background (2026-10-28 redesign); a spec's own `colour:` wins.
COLOURS = ["#FF6A3D", "#A9B2FF", "#FFB37A", "#7CE3A8", "#FF8AC2", "#8FD8FF"]


def _esc(s) -> str:
    import html
    import re
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", html.escape(str(s), quote=False))


def _ago(ts: str) -> str:
    from datetime import datetime, timezone
    try:
        secs = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
    except Exception:
        return "?"
    return f"{int(secs // 60)}m ago" if secs < 3600 else (
        f"{int(secs // 3600)}h ago" if secs < 86400 else f"{int(secs // 86400)}d ago")


def _model(name: str, a: dict) -> str:
    """What runs the agent, in words. The CTO's model lives in config.json only (its spec names none),
    so the spec's `?` and a bare `default` both read as nothing to someone looking at the panel."""
    if name == "cto":
        c = _config.load().get("cto") or {}
        m = c.get("model") or "default"
        m = "Claude Code's default model" if m == "default" else m
        return m + (f", effort {c['effort']}" if c.get("effort") not in (None, "", "default") else "")
    return a.get("model") or "?"


def team() -> dict:
    import seat
    import seat_status
    specs = seat.load_agents()
    live = {s.get("seat"): s for s in seat_status.running_seats()[0] if s.get("seat")}
    last = {}
    runs = _config.state_dir() / "agent_runs.jsonl"
    if runs.exists():
        for line in runs.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                last[r.get("agent")] = r
            except Exception:
                continue
    # config.json "owner": the human the CTO reports to (shown on the Team panel)
    out = {"owner": {"label": _config.load().get("owner") or "Owner", "colour": "#FFFFFF", "reports_to": None,
                     "role": "Owner. Approves production deploys, spend and secrets.", "kind": "owner"}}
    for i, name in enumerate(sorted(specs, key=lambda n: (n != "cto", n))):
        a = dict(specs[name])
        a["model"] = _model(name, a)
        if name in live:
            state = f"RUNNING (pid {live[name]['pid']}, {live[name]['etime']})"
        elif name in last:
            r = last[name]
            state = f"last run {_ago(r.get('ts', ''))}: exit {r.get('exit')}, {r.get('tool_calls', 0)} tool calls"
        else:
            state = "never run" if name != "cto" else "runs when you start `cto`"
        ro = " · read-only" if a.get("read_only") == "true" else ""
        where = ("the integration checkout" if name == "cto" else
                 "cards run in their own worktree" if a.get("writes") == "true" else a.get("worktree", "."))
        # `role` is the one-line summary; the same facts as fields, for a page that lays them out
        out[name] = {"label": a.get("seat", name), "colour": a.get("colour") or COLOURS[i % len(COLOURS)],
                     "reports_to": "owner" if name == "cto" else "cto",
                     "role": f"{a.get('harness', '?')} + {a.get('model', '?')} · {where}{ro} · {state}",
                     "kind": "cto" if name == "cto" else "seat", "harness": a.get("harness", "?"),
                     "model": a.get("model", "?"), "where": where, "read_only": a.get("read_only") == "true",
                     "state": state, "running": name in live}
    return out


def board() -> dict:
    import board as boardmod
    b = json.loads((REPO / ".ai-teammates" / "board.json").read_text(encoding="utf-8"))
    b["agents"] = team()                    # roster from the specs, never hand-kept
    # install.sh writes plain column ids; the page wants {id, label}. board.py accepts both.
    b["columns"] = boardmod.columns(b)
    b["project"] = _config.load().get("project") or REPO.name
    return b


def _rules_md_groups() -> list:
    """RULES.md: a header line, then indented lines; '- ' starts a new item."""
    p = REPO / ".ai-teammates" / "RULES.md"
    if not p.exists():
        return []
    groups, cur = [], None
    for raw in p.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        if not raw[0].isspace():
            cur = {"h": raw.strip().rstrip(":"), "cls": "", "li": []}
            groups.append(cur)
        elif cur is not None:
            line = raw.strip()
            if line.startswith("- ") or not cur["li"]:
                cur["li"].append(line[2:] if line.startswith("- ") else line)
            else:
                cur["li"][-1] += " " + line
    for g in groups:
        g["li"] = [_esc(x) for x in g["li"]]
    return groups


def rules() -> dict:
    import seat
    from datetime import date
    production, integration = _config.branches()
    code = lambda s: f"<code>{_esc(s)}</code>"
    flow = [f"Every change starts on a feature branch created from {code(integration)}, is merged "
            f"into {code(integration)}, tested on the development deploy, then promoted to "
            f"{code(production)}." if integration else f"Feature branch → {code(production)}.",
            f"Nothing is authored on {code(production)}: it advances only by merge "
            f"(guard.py + the git pre-push hook, which sees where a push really lands).",
            "Product paths are edited only on a feature branch: "
            + ", ".join(code(g) for g in _config.guarded()) + ".",
            "Override a push only out loud: " + code("AI_TEAMMATES_ALLOW='<why>' git push …") + " (logged)."]
    seats = [f"<b>{_esc(a.get('seat', n))}</b> — {_esc(a.get('harness'))} + {code(_model(n, a))}"
             + (" · read-only" if a.get("read_only") == "true" else "")
             for n, a in sorted(seat.load_agents().items(), key=lambda x: (x[0] != "cto", x[0]))]
    src = [p for p in (REPO / ".ai-teammates" / "RULES.md", REPO / ".ai-teammates" / "config.json") if p.exists()]
    updated = date.fromtimestamp(max(p.stat().st_mtime for p in src)).isoformat() if src else "?"
    return {"updated": updated,
            "groups": [{"h": "Branch flow (enforced by hooks)", "cls": "enforced", "li": flow},
                       {"h": "Team (from .ai-teammates/agents)", "cls": "", "li": seats}]
                      + _rules_md_groups()}


def health() -> dict:
    """Is memory CAPTURING and is the decision log fresh - not 'does a file exist'."""
    from datetime import date
    m = _config.mem_capture()
    checks = [{"check": "claude-mem capture", "stale": not m["ok"], "days": m["days"],
               "why": m["why"]}]
    try:
        upd = json.loads((REPO / ".ai-teammates" / "board.json").read_text())["updated"]
        days = (date.today() - date.fromisoformat(upd)).days
    except Exception:
        days = None
    checks.append({"check": "board.json", "stale": days is None or days > 7, "days": days})
    return {"checks": checks, "uncaptured": 0}


GENERATED = {"/board.json": board, "/rules.json": rules, "/health.json": health, "/team.json": team}


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        route = self.path.split("?")[0]
        if route in GENERATED:
            try:
                body = json.dumps(GENERATED[route](), indent=1, ensure_ascii=False).encode()
            except Exception as e:                      # never 500 the panel
                body = json.dumps({"error": f"{type(e).__name__}: {e}"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if route in DATA:
            src, ctype = DATA[route]
            try:
                body = src.read_bytes()
            except Exception as e:
                body, ctype = json.dumps({"error": str(e)}).encode(), "application/json"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if route in ("/git.json", "/git"):
            try:
                body = json.dumps(git_state(), indent=2).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            except Exception as e:                      # never 500 the panel
                body = json.dumps({"error": str(e)}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # FOUND 2026-10-29: the pilot project's bookmark, /dashboard.html (its own page until the
        # upgrade), answered 404; the page that ships is index.html at /. An old address leads there,
        # unless the folder served still has a dashboard.html of its own.
        if route == "/dashboard.html" and not (Path(self.directory) / "dashboard.html").exists():
            query = self.path[len(route):]
            self.send_response(302)
            self.send_header("Location", "/" + query)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        super().do_GET()

    def log_message(self, *a):
        pass                                            # keep launchd logs quiet


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    print(f"  dashboard: http://127.0.0.1:{port}  (serving {SERVE}; Ctrl-C to stop)", flush=True)
    ThreadingHTTPServer(
        ("127.0.0.1", port),
        partial(Handler, directory=str(SERVE)),
    ).serve_forever()
