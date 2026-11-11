#!/usr/bin/env python3
"""Launch a teammate seat, and record what it cost and whether it behaved.

    python3 .ai-teammates/hooks/seat.py list
    python3 .ai-teammates/hooks/seat.py run --card DOC-2          # the unit of delegation (cardrun.py)
    python3 .ai-teammates/hooks/seat.py land DOC-2                # merge it, its checks re-run on the merged result
    python3 .ai-teammates/hooks/seat.py verify ONB                # a parent card: the whole outcome, once all landed
    python3 .ai-teammates/hooks/seat.py card-clean DOC-2          # after merging its branch by hand
    python3 .ai-teammates/hooks/seat.py run security-audit "Audit main..development"   # free-form, read-only seats
    python3 .ai-teammates/hooks/seat.py report
    python3 .ai-teammates/hooks/seat.py report --delegation [--days N]   # product lines: seats vs the CTO
    python3 .ai-teammates/hooks/seat.py report --cost [--days N]         # spend per day, card and model
    python3 .ai-teammates/hooks/seat.py spend                            # today's spend against the daily budget

Two things this exists to answer:

  1. What did each seat consume?
  2. Did it stay inside its guardrails?

On (1): a card run records what Claude Code itself reports (total_cost_usd, turns)
next to what is measured here: wall time, tool calls, files touched, model. A
free-form run records the measured part only.

On (2), guard.py already writes every block to events.jsonl. Setting
CLAUDE_AGENT_NAME before launching is what makes those blocks attributable to a
seat instead of all landing on "main".
"""
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _config  # noqa: E402
# Seat branches start from the integration branch: the flow is
# feature -> integration -> production, and a seat branched off production
# skips the branch it must merge into (the pilot project, 2026-09-18).
BASE = _config.branches()[1] or _config.branches()[0]
GIT_BIN = "/usr/bin/git" if Path("/usr/bin/git").exists() else "git"
AGENTS = REPO / ".ai-teammates" / "agents"
RUNS = _config.state_dir() / "agent_runs.jsonl"
EVENTS = _config.state_dir() / "events.jsonl"


def load_agents():
    """Parse the YAML-ish frontmatter. Deliberately not a YAML dependency."""
    out = {}
    if not AGENTS.exists():
        return out
    for path in sorted(AGENTS.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        if not m:
            continue
        meta = {}
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                v = re.sub(r"\s+#.*$", "", v)     # inline comment is not part of the value
                meta[k.strip()] = v.strip().strip('"').strip("'")
        meta["_path"] = str(path.relative_to(REPO))
        out[meta.get("name", path.stem)] = meta
    return out


def events_for(agent, since_iso):
    """Guardrail blocks and tool calls this seat produced during the run."""
    # kinds are exactly what guard.py emits: blocked / override / warn / tool.
    # Checked against the live log rather than assumed - an earlier draft looked
    # for "block" and would have silently reported zero guardrail hits forever.
    blocks, tools, overrides, warns = [], 0, 0, 0
    if not EVENTS.exists():
        return blocks, tools, overrides, warns
    for line in EVENTS.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("agent") != agent or (e.get("ts") or "") < since_iso:
            continue
        kind = e.get("kind")
        if kind == "blocked":
            blocks.append(e.get("rule") or "?")
        elif kind == "override":
            overrides += 1
        elif kind == "warn":
            warns += 1
        elif kind == "tool":
            tools += 1
    return blocks, tools, overrides, warns


def cmd_list(agents):
    if not agents:
        print("  no seats defined in .ai-teammates/agents/")
        return
    print(f"  {'seat':<22} {'harness':<9} {'model':<24} worktree")
    for a in agents.values():
        print(f"  {a.get('seat', a['name']):<22} {a.get('harness', '?'):<9} "
              f"{a.get('model', '?'):<24} {a.get('worktree', '-')}")


def assert_not_running(name):
    """The double-run check belongs at the LAUNCH. It used to sit in `cto`
    pre-flight, which refused to open the CTO session while any seat ran -
    blocking the review of the very run it was worried about - while the
    actual launch point checked nothing. Process table, never a log."""
    import seat_status
    mine, pid = set(), str(os.getpid())          # this launch and whatever shell ran it
    while pid not in ("", "0", "1") and pid not in mine:
        mine.add(pid)
        pid = subprocess.run(["ps", "-o", "ppid=", "-p", pid],
                             capture_output=True, text=True).stdout.strip()
    live = [s for s in seat_status.running_seats()
            if s["seat"] == name and s["pid"] not in mine]
    if live:
        sys.exit(f"seat '{name}' is already running (pid {live[0]['pid']}, "
                 f"{live[0]['etime']}). A second run would double-run it.")


def cmd_run(agents, name, task):
    a = agents.get(name)
    if not a:
        sys.exit(f"unknown seat '{name}'. try: {', '.join(agents) or 'none'}")

    # Claude Code only: its hooks are what attribute, limit and stop a seat during the run.
    # Until 2026-11 any spec's command line was launched here, whatever harness it named.
    if a.get("harness") != "claude-code":
        sys.exit(f"seat '{name}' runs on '{a.get('harness', '?')}'. Seats run on claude-code only: set "
                 f"`harness: claude-code` and a `claude ...` headless command in {a.get('_path', 'its spec')}.")

    assert_not_running(name)

    # Headless is the default for delegated work: the spec's `claude -p ...` line with the task
    # appended as its prompt.
    headless = a.get("headless")
    if headless and os.environ.get("SEAT_INTERACTIVE") != "1":
        # No card sets this run's cap, so it gets the owner's maximum for one run (issue #16): an
        # audit once ran the owner's usage limit out. Right after `claude`, never after the prompt.
        if re.match(r"claude\s", headless) and "--max-budget-usd" not in headless:
            import card as cardmod
            headless = re.sub(r"^claude\b", f"claude --max-budget-usd {float(cardmod.limits()['max_usd']):g}",
                              headless)
        # shlex.quote, not json.dumps: JSON quoting is shell double-quoting,
        # so every `code span` in a markdown brief was executed by the shell.
        launch = f'{headless} {shlex.quote(task)}'
    else:
        launch = a.get("launch")
    if not launch:
        sys.exit(f"seat '{name}' has no launch command")

    wt = (REPO / a.get("worktree", ".")).resolve()
    if not wt.exists():
        # 2026-09-09: this used to exit and tell the operator to run `git
        # worktree add` by hand. That turned the workspace consolidation to
        # three worktrees (main, UI-test, LinkedIn) into four dead seats. A seat
        # worktree is scratch space for one run, not something a human should
        # have to curate - so create it here, off BASE, and let the run proceed.
        branch = f"{a.get('branch_prefix', 'feat/')}{name}-{int(time.time())}"
        print(f"  creating seat worktree {wt.name} on {branch}")
        r = subprocess.run([GIT_BIN, "worktree", "add", "-b", branch, str(wt), BASE],
                           cwd=REPO, capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"could not create seat worktree {wt}:\n{r.stderr.strip()}")

    env = dict(os.environ)
    env["CLAUDE_AGENT_NAME"] = name          # <- makes hooks attribute to this seat
    env["AI_TEAMMATES_SEAT"] = a.get("seat", name)
    # /usr/local/bin/git has been a 2021 standalone install (2.19.0) shadowing
    # Apple's 2.50.1 in /usr/bin: `--sparse` and worktrees need 2.25, and 247
    # failures sat in a harness log before anyone noticed. /usr/bin first, for
    # the seat only; fixing it system-wide is the human's call.
    env["PATH"] = "/usr/bin:" + env.get("PATH", "")

    # Bring the worktree up to date with BASE before launching. Twice now a
    # seat has been handed a spec that did not exist in its checkout: the
    # worktree sat several commits behind, and `external_directory: deny`
    # (correctly) stopped it reading the file from the main repo. The seat
    # asked instead of working around it, which is right - but it should never
    # have been put in that position.
    fetch = subprocess.run([GIT_BIN, "merge", "--ff-only", BASE],
                           cwd=str(wt), capture_output=True, text=True)
    if fetch.returncode == 0:
        print(f"  synced   {wt.name} -> {BASE}")
    else:
        behind = subprocess.run([GIT_BIN, "rev-list", "--count", f"HEAD..{BASE}"],
                                cwd=str(wt), capture_output=True, text=True)
        n = (behind.stdout or "0").strip()
        if n not in ("0", ""):
            print(f"  WARNING  {wt.name} is {n} commits behind {BASE} and could not "
                  f"fast-forward. The seat may not see recent specs.")

    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    t0 = time.time()
    print(f"  seat    {a.get('seat')}\n  model   {a.get('model')}\n"
          f"  cwd     {wt}\n  task    {task}\n")

    # Stream to the terminal AND keep a copy (scrubbed, in the state dir).
    proc = subprocess.Popen(launch, shell=True, cwd=str(wt), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    captured = []
    for line in proc.stdout:
        sys.stdout.write(line)
        captured.append(line)
    rc = proc.wait()
    elapsed = round(time.time() - t0, 1)
    out = "".join(captured)

    # Tool calls and guard hits come from events.jsonl, which the hooks write during the run.
    blocks, tools, overrides, warns = events_for(name, started)
    # Scrubbed: raw harness output echoes whatever the tools printed, and this
    # file used to be the one log that skipped activity.py's secret scrubber.
    from activity import scrub
    logs = _config.state_dir() / "seat_logs"
    logs.mkdir(exist_ok=True)
    (logs / f"{name}_{started.replace(':', '-')}.log").write_text(scrub(out), encoding="utf-8")
    row = {
        "ts": started, "agent": name, "seat": a.get("seat"),
        "harness": a.get("harness"), "model": a.get("model"),
        "task": task, "seconds": elapsed, "exit": rc,
        "tool_calls": tools, "blocks": blocks, "overrides": overrides,
        "warns": warns,
        "clean": rc == 0 and not blocks and overrides == 0,
    }
    with RUNS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"\n  {elapsed}s · exit {rc} · {tools} tool calls · "
          f"{len(blocks)} blocked · {overrides} overrides")
    if blocks:
        print("  guardrail hits: " + ", ".join(sorted(set(blocks))))
    if overrides:
        print("  ** override used — check the seat's summary explains why **")
    return rc


def cmd_report():
    if not RUNS.exists():
        print("  no runs recorded yet")
        return
    rows = [json.loads(l) for l in RUNS.read_text().splitlines() if l.strip()]
    agg = {}
    for r in rows:
        a = agg.setdefault(r["agent"], {
            "seat": r.get("seat"), "model": r.get("model"), "runs": 0,
            "secs": 0.0, "tools": 0, "blocks": 0, "ovr": 0, "clean": 0, "cost": 0.0,
            "cards": 0, "blocked": 0})
        a["runs"] += 1
        a["secs"] += r.get("seconds", 0)
        a["tools"] += r.get("tool_calls", 0)
        a["blocks"] += len(r.get("blocks") or [])
        a["ovr"] += r.get("overrides", 0)
        a["clean"] += 1 if r.get("clean") else 0
        a["cost"] += r.get("cost_usd") or 0
        a["cards"] += 1 if r.get("card") else 0
        a["blocked"] += 1 if r.get("status") == "blocked" else 0

    print(f"  {'seat':<22} {'runs':>5} {'cards':>6} {'minutes':>8} {'tools':>6} "
          f"{'blocked':>8} {'asked':>6} {'clean':>6} {'usd':>8}")
    for a in agg.values():
        print(f"  {a['seat']:<22} {a['runs']:>5} {a['cards']:>6} {a['secs']/60:>8.1f} "
              f"{a['tools']:>6} {a['blocks']:>8} {a['blocked']:>6} "
              f"{a['clean']}/{a['runs']:>3} {a['cost']:>8.3f}")
    print("\n  clean = a card run that ended `done`: a diff inside scope AND every acceptance")
    print("  command passing. Older runs: exit 0 and no guard hit. usd: Claude Code's own")
    print("  total_cost_usd; absent for free-form runs.")


def spend_line() -> str:
    """One line for the pre-flight, so the CTO's brief carries what today has cost."""
    import cardrun
    rows = cardrun.run_rows()
    now = datetime.now().astimezone()
    today = now.strftime("%Y-%m-%d")
    week = {(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)}
    cost = lambda days: sum(r.get("cost_usd") or 0 for r in rows if cardrun.local_day(r.get("ts")) in days)
    runs = sum(1 for r in rows if r.get("card") and cardrun.local_day(r.get("ts")) == today)
    daily = _config.daily_budget()
    of = f" of ${daily:.2f}" if daily else " (no daily budget)"
    return f"  spend: today ${cost({today}):.2f}{of} in {runs} card run(s) · last 7 days ${cost(week):.2f}"


def cmd_cost(days: int):
    """What card runs cost: per day, per card, per model. From the run log, which has every run;
    a card's own file has its runs too. Claude Code's figures: on a subscription they are its
    estimate of the cost, which is what counts against the plan's limit."""
    import cardrun
    since = (datetime.now().astimezone() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    rows = [r for r in cardrun.run_rows() if r.get("card") and cardrun.local_day(r.get("ts")) >= since]
    if not rows:
        print(f"  no card runs in the last {days} days")
        return

    def table(title, key):
        agg = {}
        for r in rows:
            a = agg.setdefault(key(r), {"runs": 0, "usd": 0.0, "in": 0, "out": 0, "over": 0})
            t = r.get("tokens") or {}
            a["runs"] += 1
            a["usd"] += r.get("cost_usd") or 0
            a["in"] += t.get("in") or 0
            a["out"] += t.get("out") or 0
            a["over"] += 1 if r.get("status") == "over_budget" else 0
        print(f"\n  {title:<28} {'runs':>5} {'usd':>9} {'tokens in':>11} {'tokens out':>11} {'stopped at cap':>15}")
        for k in sorted(agg, key=lambda k: -agg[k]["usd"] if title != "per day" else k):
            a = agg[k]
            print(f"  {str(k)[:28]:<28} {a['runs']:>5} {a['usd']:>9.3f} {a['in']:>11} {a['out']:>11} {a['over']:>15}")
        return agg

    print(f"  card runs, last {days} days (daily budget: "
          + (f"${_config.daily_budget():.2f}" if _config.daily_budget() else "none") + ")")
    table("per day", lambda r: cardrun.local_day(r.get("ts")))
    table("per card", lambda r: r.get("card"))
    table("per model", lambda r: r.get("model") or "?")
    print(f"\n  {'total':<28} {len(rows):>5} {sum(r.get('cost_usd') or 0 for r in rows):>9.3f}")
    billing = (_config.load().get("cto") or {}).get("billing")
    print("  " + ("API billing: these dollars are billed." if billing == "api" else
                  "On a subscription these are Claude Code's estimates: nothing is billed, but it is what "
                  "counts against your plan's limit."))
    print("  tokens in counts cached input too. Free-form seat runs and the CTO's own session are not in here.")


def cmd_delegation(days: int):
    """Product-code lines that reached the integration branch in the last N days, per day: by seats
    (commits authored seat:*, which is how the card runner commits) and by everyone else. Merges are
    left out: landing a card is not writing it. MEASURED 2026-10-17 by hand: 982 of 1,252 lines were
    the CTO's own, and the owner had no report that showed it."""
    # Filtered here, not with --since: git stops walking at the first commit older than --since, so one
    # old-dated commit on top (a cherry-pick, a rebase) hid everything below it. FOUND writing its test.
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    out = subprocess.run([GIT_BIN, "log", BASE, "--no-merges", "--numstat", "--date=short",
                          "--format=@%ad %an", "--", *_config.guarded()],
                         cwd=str(REPO), capture_output=True, text=True).stdout
    per, day, who = {}, "", ""
    for line in out.splitlines():
        if line.startswith("@"):
            day, who = line[1:].split(" ", 1)
            who = "seat" if who.startswith("seat:") else "cto"
        elif line.strip() and day >= since:
            a, d = (line.split("\t") + ["0", "0"])[:2]
            n = (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)
            per.setdefault(day, {"seat": 0, "cto": 0})[who] += n
    print(f"  product lines ({', '.join(_config.guarded())}) that reached {BASE}, last {days} days")
    print(f"  {'day':<12} {'seats':>7} {'cto':>7} {'cto %':>6}")
    tot = {"seat": 0, "cto": 0}
    for d in sorted(per):
        s, c = per[d]["seat"], per[d]["cto"]
        tot["seat"] += s
        tot["cto"] += c
        print(f"  {d:<12} {s:>7} {c:>7} {100 * c // max(s + c, 1):>5}%")
    s, c = tot["seat"], tot["cto"]
    print(f"  {'total':<12} {s:>7} {c:>7} {100 * c // max(s + c, 1):>5}%")
    print("\n  seats = commits by seat:* (card runs); cto = every other author. Merges are not counted.")


def main():
    agents = load_agents()
    argv = sys.argv[1:]
    if not argv or argv[0] == "list":
        cmd_list(agents)
    elif argv[0] == "run" and "--card" in argv:
        # python3 seat.py run [<seat>] --card <ID>: the unit of delegation is a card
        import cardrun
        i = argv.index("--card")
        cid = argv[i + 1] if len(argv) > i + 1 else ""
        pos = [x for j, x in enumerate(argv[1:], 1) if j != i and j != i + 1 and not x.startswith("--")]
        sys.exit(cardrun.run(agents, cid, pos[0] if pos else ""))
    elif argv[0] == "land" and len(argv) >= 2:
        import cardrun
        sys.exit(cardrun.land(argv[1]))
    elif argv[0] == "verify" and len(argv) >= 2:
        import cardrun
        sys.exit(cardrun.verify(argv[1]))
    elif argv[0] == "card-clean" and len(argv) >= 2:
        import cardrun
        sys.exit(cardrun.clean(argv[1], "--abandon" in argv))
    elif argv[0] == "run" and len(argv) >= 2:
        sys.exit(cmd_run(agents, argv[1], " ".join(argv[2:]) or "(no task given)"))
    elif argv[0] == "spend":
        print(spend_line())
    elif argv[0] == "report" and "--cost" in argv:
        i = argv.index("--days") if "--days" in argv else -1
        cmd_cost(int(argv[i + 1]) if 0 <= i < len(argv) - 1 and argv[i + 1].isdigit() else 7)
    elif argv[0] == "report" and "--delegation" in argv:
        i = argv.index("--days") if "--days" in argv else -1
        cmd_delegation(int(argv[i + 1]) if 0 <= i < len(argv) - 1 and argv[i + 1].isdigit() else 30)
    elif argv[0] == "report":
        cmd_report()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
