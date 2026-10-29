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

Two things this exists to answer:

  1. What did each seat consume?
  2. Did it stay inside its guardrails?

On (1), be honest about the limit: Ollama exposes no local usage or credits API
(/api/usage and /api/account both 404). So we record what is measurable here —
wall time, tool calls, files touched, model — and treat ollama.com as the
authority for currency. Wall time on a fixed model is a usable proxy for spend
and it is real; an invented token count would not be.

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
# skips the branch it must merge into (ContentOS, 2026-09-18).
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
    live = [s for s in seat_status.running_seats()[0]
            if s["seat"] == name and s["pid"] not in mine]
    if live:
        sys.exit(f"seat '{name}' is already running (pid {live[0]['pid']}, "
                 f"{live[0]['etime']}). A second run would double-run it.")


def cmd_run(agents, name, task):
    a = agents.get(name)
    if not a:
        sys.exit(f"unknown seat '{name}'. try: {', '.join(agents) or 'none'}")

    assert_not_running(name)

    # Headless is the default for delegated work: `opencode run "<task>"` behind
    # an `--` separator, which is what ollama launch needs to pass args through.
    # The model tag must carry :cloud -- without it ollama tries to resolve a
    # LOCAL model and offers to pull 18GB, which breaks the no-local-models rule.
    headless = a.get("headless")
    if headless and os.environ.get("SEAT_INTERACTIVE") != "1":
        # {DIR} is substituted below. opencode does NOT inherit the parent
        # process cwd - measured, not assumed: without --dir it wrote PROOF.txt
        # into the main repo while running with cwd set to a scratch dir. That
        # single defect is why earlier delegated runs looked like no-ops.
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
    # Claude Code does not recognise Ollama model names and assumes a 200k
    # window. The spec's context_window was documented as "exported" and never
    # was - so the audit seat ran truncated. Now it is.
    if a.get("harness") == "claude-code" and a.get("context_window"):
        env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = a["context_window"]

    # /usr/local/bin/git is a 2021 standalone install (2.19.0) that shadows
    # Apple's 2.50.1 in /usr/bin. OpenCode snapshots files before every edit
    # with `git add --sparse`, which landed in git 2.25 - against 2.19 it fails
    # with "unknown option `sparse'" and the run stalls. 247 such failures were
    # sitting in the opencode log. Put /usr/bin first for the seat only; fixing
    # it system-wide means removing a root-owned symlink, which is the human's call.
    env["PATH"] = "/usr/bin:" + env.get("PATH", "")

    # `ollama launch claude` routes Claude Code at an Ollama-hosted model. If
    # ANTHROPIC_API_KEY is set it takes precedence, so the seat would quietly
    # run on Anthropic instead - wrong model, and it spends Anthropic credit
    # while the run reports success. Strip it for seats on the ollama path.
    if "ollama launch" in launch:
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)

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

    launch = launch.replace("{DIR}", shlex.quote(str(wt)))

    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    t0 = time.time()
    print(f"  seat    {a.get('seat')}\n  model   {a.get('model')}\n"
          f"  cwd     {wt}\n  task    {task}\n")

    # Stream to the terminal AND keep a copy: opencode and codex do not write
    # to events.jsonl (that is an activity.py hook, and only Claude Code fires
    # hooks). Counting from events.jsonl alone reported tool_calls=0 for a run
    # that plainly did work - the same defect class as matching kind=="block"
    # when guard emits "blocked": a monitor pointed at the wrong source reports
    # zero forever and looks healthy doing it.
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

    # Strip ANSI FIRST. The marker is preceded by a colour escape, so "^\s*"
    # never reached it and every run recorded tool_calls=0 while an 85KB log
    # sat on disk showing 66 tool lines. Third time a counter has been pointed
    # at something it could not see.
    plain = re.sub(r"\x1b\[[0-9;]*m", "", out)
    blocks, tools, overrides, warns = events_for(name, started)
    if tools == 0:
        tools = len(re.findall(r"^\s*(?:→|✱|\$)\s", plain, re.M))
    # opencode reports a refused action in its own output, not to our log
    for m in re.finditer(r'permission[^\n]*?"([^"]+)"[^\n]*?disallow|'
                         r'deny rule for `([^`]+)`', plain, re.I):
        blocks.append(next(g for g in m.groups() if g))
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
    print("  total_cost_usd; absent for runs on other harnesses.")


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
    elif argv[0] == "report" and "--delegation" in argv:
        i = argv.index("--days") if "--days" in argv else -1
        cmd_delegation(int(argv[i + 1]) if 0 <= i < len(argv) - 1 and argv[i + 1].isdigit() else 30)
    elif argv[0] == "report":
        cmd_report()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
