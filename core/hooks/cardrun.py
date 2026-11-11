#!/usr/bin/env python3
"""Run one task card on a seat, and report what happened as FACTS.

    python3 .ai-teammates/hooks/seat.py run <seat> --card <ID>     (dispatches here)
    python3 .ai-teammates/hooks/seat.py land <ID>                   merge it, checked on the merged result
    python3 .ai-teammates/hooks/seat.py verify <PARENT>             the whole outcome, once every child landed
    python3 .ai-teammates/hooks/seat.py card-clean <ID> [--abandon]

What this replaces: `seat.py run <seat> "<one line>"`, which launched a harness and
recorded `clean = exit 0 and no guard hits`. On the pilot project that marked a seat that
changed nothing as clean, joined a whole spec into one string, never linked a run
to a board card, reused one scratch worktree forever, and could not stop a run.

A card run is judged by what can be checked, not by what the seat says:

  before spending anything   the card must validate (size gate), the seat must exist
  during the run             Claude Code seat, hooks live: edits outside the card's
                             scope are denied (guard.py), a human-only blocker ends
                             the run (loopguard.py), and this watchdog enforces the
                             card's wall-time and tool-call budget
  after the run              changed files are computed from git, compared to the
                             scope, every acceptance command is EXECUTED, and only
                             then is the work committed

Status: done | blocked | failed | timeout | out_of_scope | no_change.
`clean` means done. Nothing else.
"""
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _config  # noqa: E402
import board  # noqa: E402
import card as cardmod  # noqa: E402
import loopguard  # noqa: E402
import seat  # noqa: E402
from activity import scrub  # noqa: E402

REPO = seat.REPO
GIT = seat.GIT_BIN
BASE = seat.BASE
# The test suite points this at a stub so the whole pipeline runs without a model.
CLAUDE = os.environ.get("AI_TEAMMATES_CLAUDE_BIN", "claude")

# Verified to resolve through `claude --model` on 2026-10-04, with the cost of a one-word reply:
# Haiku 4.5 $0.058, Sonnet 5.5 $0.082, Opus 5.5 $0.163 (the 5.0 models they replace: Sonnet 5
# $0.13, Opus 5 $0.21, so the newer ones are also cheaper). Override in config.json "seat_models".
DEFAULT_MODELS = {"mechanical": "claude-haiku-4-5-20251001",
                  "standard": "claude-sonnet-5-5",
                  "hard": "claude-opus-5-5"}
EXIT = {"done": 0, "blocked": 75}      # anything else exits 1
# What Claude Code reports when --max-budget-usd stopped the run. Read from one real call on
# 2.1.285 (2026-11-10): {"subtype": "error_max_budget_usd", "is_error": true,
# "terminal_reason": "budget_exhausted", "total_cost_usd": ...}, exit 1.
BUDGET_STOP = ("error_max_budget_usd", "budget_exhausted")
BLOCKED_LINE = re.compile(r"^\s*BLOCKED:\s*([\w:.-]+)\s*\|\s*(.+?)\s*$", re.M)


def git(*args, cwd=None):
    return subprocess.run([GIT, *args], cwd=str(cwd or REPO), capture_output=True, text=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def model_for(card: dict, spec: dict) -> str:
    if card.get("model"):
        return card["model"]
    tiers = {**DEFAULT_MODELS, **(_config.load().get("seat_models") or {})}
    return tiers.get(card.get("tier") or "standard") or spec.get("model") or DEFAULT_MODELS["standard"]


def card_command(spec: dict, card: dict, prompt: str, model: str) -> list:
    """The exact argv a seat is launched with. Built here, not templated in a spec, so
    the permission posture cannot drift away from what guardrail_test asserts."""
    b = cardmod.budget(card)
    read_only = spec.get("read_only") == "true" or bool(card.get("read_only"))
    base = spec.get("allow") or ("Read,Glob,Grep" if read_only else "Read,Glob,Grep,Edit,Write")
    allow = [t.strip() for t in base.split(",") if t.strip()] + list(card.get("allow") or [])
    if not read_only:
        # the sanctioned self-check: runs the card's own acceptance commands (card.py accept)
        allow.append("Bash(python3 .ai-teammates/hooks/card.py accept:*)")
        # deleting a file the card names (guard.py refuses any file outside the card)
        allow.append("Bash(git rm:*)")
    # Offered = allowed. MEASURED 2026-10-06: a session is offered 24 built-in tools plus a skill
    # and a subagent listing by default (26.6k tokens on a one-word Opus call; 9.9k with a named set);
    # a seat uses a handful, and a narrow junior is better off not reading the rest.
    tools = list(dict.fromkeys(t.split("(", 1)[0].strip() for t in allow if t.strip()))
    return [CLAUDE, "-p", prompt, "--model", model,
            "--tools", ",".join(tools),
            # Turns are not the budget (a tool call is not a turn); the watchdog counts
            # calls. This is only a backstop against a model that talks forever.
            "--max-turns", str(int(b["tool_calls"]) + 10),
            # The card's money: Claude Code stops the run itself when it is spent (issue #16).
            "--max-budget-usd", f"{float(b['usd']):g}",
            "--permission-mode", "plan" if read_only else "acceptEdits",
            "--permission-prompts", "none",
            "--output-format", "stream-json", "--verbose",
            # No user plugins, user hooks or MCP servers: a seat sees the card and
            # nothing else. Memory reaches it only as the slice the CTO put on the card.
            "--setting-sources", "project,local", "--strict-mcp-config",
            "--allowedTools", ",".join(allow)]   # variadic: keep it last


# --- money ----------------------------------------------------------------------------

def tokens(res) -> dict:
    """{"in", "out"} for a run, from the per-model usage Claude Code reports with its result.
    "in" counts cached input too: it is what the model read. (The top-level `usage` of a run that
    ended in an error is all zeros; `modelUsage` is not.)"""
    out = {"in": 0, "out": 0}
    for u in ((res or {}).get("modelUsage") or {}).values():
        if isinstance(u, dict):
            out["in"] += sum(int(u.get(k) or 0) for k in ("inputTokens", "cacheReadInputTokens",
                                                          "cacheCreationInputTokens"))
            out["out"] += int(u.get("outputTokens") or 0)
    return out


def local_day(ts: str) -> str:
    """The owner's calendar day a run started on (rows are stamped in UTC)."""
    try:
        return datetime.fromisoformat(ts).astimezone().strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def run_rows() -> list:
    rows = []
    if seat.RUNS.exists():
        for line in seat.RUNS.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def spent_on(day: str) -> float:
    return sum(r.get("cost_usd") or 0 for r in run_rows() if local_day(r.get("ts")) == day)


def over_daily_budget(cid: str, cap: float) -> str:
    """Why this card may not be launched today, or "". The budget across runs is the owner's
    (config.json budget.daily_usd): one card's cap bounds one run, and nothing bounded the day."""
    daily = _config.daily_budget()
    if not daily:
        return ""
    spent = spent_on(datetime.now().astimezone().strftime("%Y-%m-%d"))
    if spent + cap <= daily:
        return ""
    return (f"today's card runs have cost ${spent:.2f} of the ${daily:.2f} daily budget, and card {cid} "
            f"may spend up to ${cap:g}")


# --- worktree -------------------------------------------------------------------------

def card_worktree(cid: str) -> Path:
    return REPO.parent / f"{REPO.name}-card-{cid.lower()}"


def card_branch(spec: dict, cid: str) -> str:
    return f"{spec.get('branch_prefix', 'feat/')}{cid.lower()}"


def last_run(cid: str):
    last = None
    if seat.RUNS.exists():
        for line in seat.RUNS.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("card") == cid:
                last = r
    return last


def merged(cid: str) -> bool:
    """A card has landed when its last run is done and that commit is in the integration branch."""
    r = last_run(cid)
    return bool(r and r.get("status") == "done" and r.get("head")
                and git("merge-base", "--is-ancestor", r["head"], BASE).returncode == 0)


def plan_status(cid: str) -> str:
    """written | running | merged | the last run's status. Running is read from the process table."""
    ps = subprocess.run(["ps", "-eo", "command"], capture_output=True, text=True).stdout
    if re.search(rf"seat\.py run\b.*--card {re.escape(cid)}(\s|$)", ps, re.M):
        return "running"
    r = last_run(cid)
    return "written" if not r else "merged" if merged(cid) else r.get("status") or "?"


def archive(wt: Path, branch: str, scope=None) -> str:
    """Tag, then delete. A tag costs nothing and makes the deletion reversible.

    The seat's uncommitted work is committed to the branch first (only the files the card
    names: a blanket add would sweep in the node_modules link), otherwise the tag would
    point at a branch with none of it and `worktree remove --force` would silently drop
    it. FOUND on the DOC-2 pilot: a correct blocked attempt would have been lost."""
    if wt.exists() and scope:
        git("add", "-A", "--", *scope, cwd=wt)
        if git("diff", "--cached", "--name-only", cwd=wt).stdout.strip():
            git("-c", "user.name=seat:archive", "-c", "user.email=archive@seat.local", "commit", "-q",
                "-m", "WIP: unfinished card attempt, archived before a re-run", cwd=wt)
    tag = f"archive/{branch}-{int(time.time())}"
    if git("rev-parse", "--verify", "--quiet", branch).returncode == 0:
        git("tag", tag, branch)
    if wt.exists():
        git("worktree", "remove", "--force", str(wt))
    git("worktree", "prune")
    git("branch", "-D", branch)
    return tag


def make_worktree(spec: dict, cid: str, card: dict = None) -> Path:
    wt, br = card_worktree(cid), card_branch(spec, cid)
    prev = last_run(cid)
    if (wt.exists() or git("rev-parse", "--verify", "--quiet", br).returncode == 0):
        if prev and prev.get("status") == "done" and git("merge-base", "--is-ancestor", br, BASE).returncode != 0:
            sys.exit(f"card {cid} already ran to 'done' on {br} and is not merged into {BASE} yet.\n"
                     f"  Review and merge it, then:  seat.py card-clean {cid}\n"
                     f"  Or discard it:              seat.py card-clean {cid} --abandon")
        tag = archive(wt, br, cardmod.edit_scope(card) if card else None)
        print(f"  earlier attempt archived as tag {tag}")
    r = git("worktree", "add", "-b", br, str(wt), BASE)
    if r.returncode != 0:
        sys.exit(f"could not create worktree {wt}:\n{r.stderr.strip()}")
    return wt


def porcelain_paths(wt: Path) -> set:
    out = git("status", "--porcelain", "--untracked-files=all", cwd=wt).stdout
    paths = set()
    for line in out.splitlines():
        p = line[3:].strip().strip('"')
        paths.add(p.split(" -> ")[-1])
    return paths


def run_setup(wt: Path) -> str:
    """Per-worktree files git does not carry (config.json "seat_setup": shell
    commands, {REPO} = the CTO's checkout). Returns '' or what failed. A seat that
    cannot run its acceptance check must fail here, before it costs anything."""
    for cmd in _config.load().get("seat_setup") or []:
        r = subprocess.run(["/bin/sh", "-c", cmd.replace("{REPO}", str(REPO))], cwd=str(wt),
                           capture_output=True, text=True)
        if r.returncode != 0:
            return f"seat_setup failed: {cmd} -> {(r.stderr or r.stdout).strip()[:120]}"
    return ""


# --- supervised run -----------------------------------------------------------------------

def _summ(name: str, ti: dict) -> str:
    ti = ti or {}
    if name == "Bash":
        return scrub(" ".join((ti.get("command") or "").split()))[:70]
    for k in ("file_path", "path", "pattern", "notebook_path"):
        if ti.get(k):
            return str(ti[k]).replace(str(REPO.parent) + "/", "")[:70]
    return ""


def _kill(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def supervise(cmd, wt, env, minutes, max_calls, idle_s):
    """Run the seat in its own process group and enforce its budget. Python, because
    macOS has no `timeout`; and a group, because the harness spawns children."""
    proc = subprocess.Popen(cmd, cwd=str(wt), env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            bufsize=1, start_new_session=True)
    st = {"lines": [], "err": [], "tools": 0, "last": time.time(), "result": None, "text": ""}

    def pump():
        for line in proc.stdout:
            st["last"] = time.time()
            st["lines"].append(line)
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") == "assistant":
                for blk in (e.get("message") or {}).get("content") or []:
                    if blk.get("type") == "tool_use":
                        st["tools"] += 1
                        print(f"    -> {blk.get('name')} {_summ(blk.get('name'), blk.get('input'))}", flush=True)
                    elif blk.get("type") == "text" and blk.get("text"):
                        st["text"] = blk["text"]
            elif e.get("type") == "result":
                st["result"] = e

    threads = [threading.Thread(target=pump, daemon=True),
               threading.Thread(target=lambda: st["err"].extend(proc.stderr), daemon=True)]
    for t in threads:
        t.start()
    t0, reason = time.time(), ""
    while proc.poll() is None:
        time.sleep(0.4)
        if time.time() - t0 > minutes * 60:
            reason = f"over its {minutes:g} minute budget"
        elif st["tools"] > max_calls:
            reason = f"over its {max_calls} tool-call budget"
        elif time.time() - st["last"] > idle_s:
            reason = f"idle for {idle_s / 60:g} minutes"
        if reason:
            _kill(proc)
            break
    rc = proc.wait()
    for t in threads:
        t.join(timeout=5)
    st.update(rc=rc, reason=reason)
    return st


# --- the run -----------------------------------------------------------------------------

def run(agents: dict, cid: str, seat_name: str = "") -> int:
    try:
        card = cardmod.load(cid)
    except Exception as e:
        sys.exit(f"{e}")
    problems = cardmod.validate(card, cid)
    if problems:
        print(f"  card {cid} is not ready to run ({len(problems)} problem(s)). Nothing was launched:")
        for p in problems:
            print(f"    - {p}")
        return 1
    if cardmod.is_parent(card):
        print(f"  card {cid} is a parent card: the plan, never run on a seat. Nothing was launched.\n"
              f"  Run its children: {', '.join(card['children'])}  (card.py tree {cid})")
        return 1
    # A card's worktree is cut from BASE, so it sees a dependency's work only once that has merged
    waiting = [d for d in card.get("depends_on") or [] if not merged(d)]
    if waiting:
        print(f"  card {cid} depends on {', '.join(waiting)}, not merged into {BASE} yet. Nothing was launched.\n"
              f"  Its worktree would not contain that work: land {waiting[0]} first.")
        return 1
    name = seat_name or card["seat"]
    spec = agents.get(name)
    if not spec:
        sys.exit(f"unknown seat '{name}'. try: {', '.join(agents) or 'none'}")
    if spec.get("harness") != "claude-code":
        sys.exit(f"seat '{name}' runs on '{spec.get('harness')}'. Cards run on Claude Code seats: "
                 f"that is where scope, loop and budget are enforced during the run.")
    read_only = spec.get("read_only") == "true" or bool(card.get("read_only"))
    if not read_only and spec.get("writes") != "true":
        sys.exit(f"seat '{name}' is not marked `writes: true` in its spec, and the card changes files.")
    seat.assert_not_running(name)

    model = model_for(card, spec)
    b = cardmod.budget(card)
    broke = over_daily_budget(cid, float(b["usd"]))
    if broke:
        # Not launched, nothing spent. The owner's to decide, so it is recorded like any other ask
        # only a human can answer, and reaches their next session.
        need = (f"raise budget.daily_usd in .ai-teammates/config.json (or run: cto setup), "
                f"or lower card {cid}'s budget.usd, or wait until tomorrow")
        loopguard.resolve_card(cid)
        loopguard.make_record(f"budget-{cid}", {}, "budget", "human", broke, tried=[], need=need,
                              card=cid, who="cto")
        print(f"  card {cid} was not launched: {broke}.\n  BLOCKED: budget | {need}")
        return EXIT["blocked"]
    wt = make_worktree(spec, cid, card)
    br = card_branch(spec, cid)
    started = now_iso()
    t0 = time.time()
    board.set_state(cid, "doing")
    print(f"  card    {cid}: {card.get('title') or card['goal'][:60]}\n  seat    {spec.get('seat', name)} · {model}"
          f"\n  cwd     {wt}\n  budget  {b['minutes']:g} min / {b['tool_calls']} tool calls / ${float(b['usd']):g}\n")
    loopguard.resolve_card(cid)             # re-running a blocked card means the human dealt with it

    status, reason, rc, res, text, tools = "failed", "", 1, None, "", 0
    denied = []
    changed, stat, acc, head = [], "", [], ""
    setup_err = run_setup(wt)
    baseline = porcelain_paths(wt)
    lines, err = [], []
    if setup_err:
        reason = setup_err
    else:
        env = dict(os.environ)
        env.update(CLAUDE_AGENT_NAME=name, AI_TEAMMATES_SEAT=spec.get("seat", name),
                   AI_TEAMMATES_CARD=str(cardmod.path(cid)), AI_TEAMMATES_HEADLESS="1")
        env["PATH"] = "/usr/bin:" + env.get("PATH", "")
        st = supervise(card_command(spec, card, cardmod.render(card), model), wt, env,
                       b["minutes"], b["tool_calls"], cardmod.limits()["idle_minutes"] * 60)
        rc, res, tools, lines, err = st["rc"], st["result"], st["tools"], st["lines"], st["err"]
        text = (res or {}).get("result") or st["text"] or ""
        # An interrupted or killed run has no final text: the hook's record has the ask.
        mine = [d for d in loopguard.unresolved(since=started) if d.get("card") == cid]
        sentinel = BLOCKED_LINE.search(text)
        # A refused command is only a BLOCK if the run did not end normally. FOUND on the
        # DOC-2 pilot: a seat finished correctly, then tried an optional self-check it was
        # not allowed to run; calling that "blocked" threw away a correct result. What
        # decides is the run: it said BLOCKED, it was interrupted/failed, or it ended well.
        abnormal = rc != 0 or res is None or bool(res.get("is_error"))
        denied = [d["evidence"].replace("approval needed for: ", "") for d in mine]

        if st["reason"]:
            status, reason = "timeout", f"killed: {st['reason']}"
        elif res and (res.get("subtype") in BUDGET_STOP or res.get("terminal_reason") in BUDGET_STOP):
            # Claude Code stopped it at the card's cap. The work is unfinished by definition:
            # nothing is committed, and the money already spent is on the card.
            spent = res.get("total_cost_usd")
            status = "over_budget"
            reason = (f"stopped at the card's cap of ${float(b['usd']):g}"
                      + (f" (spent ${spent:.3f})" if isinstance(spent, (int, float)) else "")
                      + ": split the card, give it a cheaper tier, or raise budget.usd within the owner's maximum")
        elif sentinel or (mine and abnormal):
            status = "blocked"
            reason = (f"{sentinel.group(1)} | {sentinel.group(2)}" if sentinel
                      else f"{mine[-1]['class']} | {mine[-1]['need']}")
            if sentinel and not mine:
                # The seat stopped by itself, so no hook wrote a record. Persist its ask:
                # it must reach the human's next session, not scroll away with this one.
                loopguard.make_record(f"card-{cid}", {}, sentinel.group(1), "human", text,
                                      need=sentinel.group(2), card=cid, who=name)
        elif rc != 0 or (res or {}).get("is_error"):
            status, reason = "failed", f"harness exit {rc}" + (f" ({res.get('subtype')})" if res else " (no result)")
        elif read_only:
            status, reason = ("done", "") if text.strip() else ("failed", "no report returned")
        else:
            new = sorted(p for p in porcelain_paths(wt) if p not in baseline)
            if new:
                git("add", "--", *new, cwd=wt)
            changed = sorted(l for l in git("diff", "--cached", "--name-only", cwd=wt).stdout.splitlines() if l)
            stat = git("diff", "--cached", "--shortstat", cwd=wt).stdout.strip()
            outside = [p for p in changed if not cardmod.allowed_edit(card, p)]
            if not changed:
                status, reason = "no_change", "the seat exited cleanly and changed nothing"
            elif outside:
                status, reason = "out_of_scope", "changed outside the card: " + ", ".join(outside[:4])
            else:
                acc = cardmod.run_acceptance(card, wt)
                failed = [a for a in acc if a["rc"] != 0]
                if failed:
                    status = "failed"
                    reason = f"acceptance failed: {failed[0]['run'][:60]} (exit {failed[0]['rc']})"
                else:
                    status = "done"

    if status == "done" and not read_only:
        r = git("-c", f"user.name=seat:{name}", "-c", f"user.email={name}@seat.local", "commit", "-q", "-m",
                f"{cid}: {card.get('title') or card['goal'][:60]}\n\nCard run by seat '{name}' on {model}.\n"
                f"Acceptance: {len(acc)}/{len(acc)} passed. Files: {', '.join(changed)}.", cwd=wt)
        if r.returncode != 0:
            status, reason = "failed", f"commit failed: {(r.stderr or r.stdout).strip()[:100]}"
        else:
            head = git("rev-parse", "--short", "HEAD", cwd=wt).stdout.strip()

    elapsed = round(time.time() - t0, 1)
    if status != "blocked":
        loopguard.resolve_card(cid)        # a refused self-check that did not stop the run is not an open ask
    cost = (res or {}).get("total_cost_usd")
    # A run that ENDED normally is judged by its checks: the cap is tested between steps, so the
    # last step can pass it. Said out loud, never a reason to throw away work that passed.
    over_cap = status != "over_budget" and isinstance(cost, (int, float)) and cost > float(b["usd"])
    blocks, _, overrides, warns = seat.events_for(name, started)

    logs = _config.state_dir() / "seat_logs"
    logs.mkdir(exist_ok=True)
    (logs / f"{cid}_{started.replace(':', '-')}.log").write_text(
        scrub("".join(lines)) + ("\n--- stderr ---\n" + scrub("".join(err)) if err else ""), encoding="utf-8")
    if read_only and text.strip():
        rep = _config.state_dir() / "reports"
        rep.mkdir(exist_ok=True)
        (rep / f"{cid}_{started.replace(':', '-')}.md").write_text(scrub(text), encoding="utf-8")

    row = {"ts": started, "agent": name, "seat": spec.get("seat"), "harness": "claude-code", "model": model,
           "task": card["goal"], "card": cid, "status": status, "reason": reason, "seconds": elapsed,
           "exit": rc, "tool_calls": tools, "turns": (res or {}).get("num_turns"), "cost_usd": cost,
           "cap_usd": float(b["usd"]), "over_cap": over_cap, "tokens": tokens(res),
           "branch": br, "head": head, "files": changed, "diffstat": stat,
           "accept": [{"run": a["run"], "rc": a["rc"]} for a in acc],
           "blocks": blocks, "overrides": overrides, "warns": warns, "denied": denied, "clean": status == "done"}
    with seat.RUNS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    # ...and on the card itself: what it cost is part of the ticket, not only of a log in the git dir.
    cardmod.record_run(cid, {"ts": started, "seat": name, "model": model, "status": status,
                             "seconds": elapsed, "tool_calls": tools, "cost_usd": cost,
                             "cap_usd": float(b["usd"]), "tokens": row["tokens"]})

    passed = sum(1 for a in acc if a["rc"] == 0)
    money = f" · ${cost:.3f}" if isinstance(cost, (int, float)) else ""
    line = (f"{status} · " + (f"{len(changed)} file(s) {stat} · " if changed else "")
            + (f"acceptance {passed}/{len(acc)} · " if acc else "") + f"{elapsed:g}s · {tools} tool calls{money}"
            + (f" · {br} @ {head}" if head else ""))
    board.set_state(cid, {"done": "review"}.get(status, "blocked"), f"run {started[:16]}Z: {line}"
                    + (f" | {reason}" if reason else ""))
    print(f"\n  CARD {cid}: {line}")
    if reason:
        print(f"  reason: {reason}")
    if over_cap:
        print(f"  note: the run cost ${cost:.3f}, over its cap of ${float(b['usd']):g} on its last step. "
              f"It ended normally and was judged by its checks.")
    if denied and status != "blocked":
        print(f"  note: the seat tried {len(denied)} command(s) it was not allowed to run, was refused, and carried on: "
              + "; ".join(d[:60] for d in denied[:3]))
    for a in acc:
        if a["rc"] != 0:
            print(f"  FAILED  {a['run']}\n          {a['tail'][-160:]}")
    if status == "blocked":
        print("\n  The seat stopped and is asking for you (it did not work around it):")
        loopguard.report(started)
    elif status == "done" and not read_only:
        print(f"  worktree {wt}\n  Review it, merge {br} into {BASE}, then: seat.py card-clean {cid}")
    elif status == "done":
        print(f"  report saved in the state dir: reports/{cid}_*.md")
    elif wt.exists():
        print(f"  worktree kept for inspection: {wt}")
    if overrides:
        print("  ** an override was used during this run **")
    return EXIT.get(status, 1)


def clean(cid: str, abandon: bool) -> int:
    agents = seat.load_agents()
    try:
        card = cardmod.load(cid)
        spec = agents.get(card["seat"]) or {}
    except Exception:
        card, spec = {}, {}
    wt, br = card_worktree(cid), card_branch(spec, cid)
    if git("rev-parse", "--verify", "--quiet", br).returncode != 0 and not wt.exists():
        print(f"  nothing to clean for {cid}")
        return 0
    merged = git("merge-base", "--is-ancestor", br, BASE).returncode == 0
    if not merged and not abandon:
        print(f"  {br} is NOT merged into {BASE}. Merge it, or discard with --abandon (it is tagged first).")
        return 1
    # the seat's uncommitted in-scope work goes onto the tag too, as on a re-run. FOUND 2026-10-28 (repo
    # analysis): without the scope it was dropped with the worktree, and "tagged first" kept nothing of it
    tag = archive(wt, br, cardmod.edit_scope(card) if card else None) if not merged else ""
    if merged:
        git("worktree", "remove", "--force", str(wt))
        git("worktree", "prune")
        git("branch", "-d", br)
    print(f"  cleaned {cid}: worktree and branch removed" + (f" (kept as tag {tag})" if tag else " (it was merged)"))
    if merged:
        # memory should grow from what was delivered, not from what somebody remembered to write
        # down. Opt-in (config memory.auto_file) because it writes to the shared palace; and a
        # memory that is down must never undo a merge-and-clean that already happened.
        try:
            import memlife
            if memlife.conf().get("auto_file"):
                print(memlife.file_card(cid))
        except Exception as e:
            print(f"  (not filed to memory: {e}; run `memlife.py file {cid}` when it is back)")
    return 0


# --- landing -------------------------------------------------------------------------------

def integration_checkout():
    """The worktree that has BASE checked out, if any: its files must move with the branch."""
    cur = None
    for line in git("worktree", "list", "--porcelain").stdout.splitlines():
        if line.startswith("worktree "):
            cur = Path(line[9:])
        elif line == f"branch refs/heads/{BASE}":
            return cur
    return None


def annotate(cid: str, **facts) -> None:
    """Add facts to the card's last run row: how it landed is part of that run's record."""
    lines = seat.RUNS.read_text(encoding="utf-8").splitlines()
    for i in range(len(lines) - 1, -1, -1):
        try:
            r = json.loads(lines[i])
        except ValueError:
            continue
        if r.get("card") == cid:
            lines[i] = json.dumps({**r, **facts}, ensure_ascii=False)
            break
    seat.RUNS.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _drop(wt: Path) -> None:
    if wt.exists():
        git("worktree", "remove", "--force", str(wt))
    git("worktree", "prune")


def checks_on(wt: Path, card: dict, extra=()) -> tuple:
    """Run the card's acceptance, then the project's extra checks, in wt: (results, '' or what failed)."""
    err = run_setup(wt)
    if err:
        return [], err
    own = list(card.get("acceptance") or [])
    acc = cardmod.run_acceptance({"acceptance": own + [{"run": x} for x in extra]}, wt)
    for i, a in enumerate(acc):
        if a["rc"] != 0:
            what = "acceptance" if i < len(own) else "integration check"
            return acc, f"{what} failed on the merged result: {a['run'][:60]} (exit {a['rc']}): {a['tail'][-100:]}"
    return acc, ""


def land(cid: str) -> int:
    """Merge a done card into BASE only if its checks pass on the MERGED result.

    It used to be the CTO typing `git merge` (cto.md step 4), and nothing re-ran the card's checks
    after it: the card's worktree was cut from BASE when it started, and BASE may have moved since.
    The merge happens in a scratch worktree; BASE moves only when everything passed there, so a
    failure or a conflict leaves it exactly as it was. A conflict is never resolved here.
    config.json "integration_checks" run there too: two cards can each pass alone and break each
    other, and this is where the second one is caught. Landing a parent's last child verifies it."""
    try:
        card = cardmod.load(cid)
    except Exception as e:
        sys.exit(f"{e}")
    spec = seat.load_agents().get(card.get("seat")) or {}
    br, prev, co = card_branch(spec, cid), last_run(cid), integration_checkout()
    if merged(cid):
        print(f"  {cid} is already merged into {BASE}")
        return clean(cid, False)
    waiting = [d for d in card.get("depends_on") or [] if not merged(d)]
    refuse = (f"its last run is {(prev or {}).get('status') or 'missing'}, not done" if not prev
              or prev.get("status") != "done" or not prev.get("head")
              else f"it depends on {', '.join(waiting)}, not merged into {BASE} yet" if waiting
              # The session record (board, cards...) is written there by the runner itself during a
              # run and committed at session end: it is not a reason to refuse. FOUND 2026-11-10.
              else f"the integration checkout {co} has uncommitted changes"
              if co and git("status", "--porcelain", "--untracked-files=no", "--", ".",
                            *(f":(exclude){r.rstrip('/')}" for r in _config.RECORD), cwd=co).stdout.strip() else "")
    if refuse:
        print(f"  card {cid} cannot land: {refuse}. Nothing was merged.")
        return 1

    tip = git("rev-parse", BASE).stdout.strip()
    scratch = REPO.parent / f"{REPO.name}-land-{cid.lower()}"
    _drop(scratch)
    r = git("worktree", "add", "--detach", str(scratch), tip)
    if r.returncode != 0:
        sys.exit(f"could not create the scratch worktree {scratch}:\n{r.stderr.strip()}")
    status, reason, acc, sha = "failed", "", [], ""
    try:
        m = git("merge", "--no-ff", "-q", "-m", f"Land {cid}: {card.get('title') or card['goal'][:60]}", br, cwd=scratch)
        conflicts = git("diff", "--name-only", "--diff-filter=U", cwd=scratch).stdout.split()
        if conflicts:
            git("merge", "--abort", cwd=scratch)
            status, reason = "blocked", f"merge-conflict | {', '.join(conflicts)}"
            loopguard.make_record(f"land-{cid}", {}, "merge-conflict", "human",
                                  f"merging {br} into {BASE} conflicts in {', '.join(conflicts)}", tried=[],
                                  need=f"resolve the merge of {br} into {BASE} by hand: {', '.join(conflicts)}",
                                  card=cid, who="cto")
        elif m.returncode != 0:
            reason = f"merge failed: {(m.stderr or m.stdout).strip()[:120]}"
        else:
            acc, reason = checks_on(scratch, card, _config.load().get("integration_checks") or [])
            if not reason:
                sha = git("rev-parse", "HEAD", cwd=scratch).stdout.strip()
                adv = (git("merge", "--ff-only", "-q", sha, cwd=co) if co
                       else git("update-ref", f"refs/heads/{BASE}", sha, tip))
                if adv.returncode != 0:
                    reason = f"{BASE} could not move to the merge: {(adv.stderr or adv.stdout).strip()[:120]}"
                else:
                    status = "merged"
    finally:
        _drop(scratch)

    passed = sum(1 for a in acc if a["rc"] == 0)
    facts = {"status": status, "ts": now_iso(), "reason": reason, "accept": [{"run": a["run"], "rc": a["rc"]} for a in acc]}
    if status == "merged":
        annotate(cid, merged=sha, land=facts)
        board.set_state(cid, "done", f"landed {facts['ts'][:16]}Z: {sha[:8]} on {BASE}, "
                                     f"acceptance {passed}/{len(acc)} on the merged result")
        print(f"  LANDED {cid}: {br} merged into {BASE} @ {sha[:8]} · checks {passed}/{len(acc)} on the merged result")
        rc, par = clean(cid, False), card.get("parent")
        try:
            kids = cardmod.load(par).get("children") or [] if par else []
        except Exception:
            kids = []
        if kids and all(merged(k) for k in kids):
            print(f"  {cid} was the last child of {par} to land: verifying the whole outcome")
            return verify(par) or rc
        return rc
    annotate(cid, land=facts)
    board.set_state(cid, "blocked", f"land {facts['ts'][:16]}Z: {status} | {reason}"[:300])
    print(f"  NOT LANDED {cid}: {reason}\n  {BASE} is unchanged.")
    if status == "blocked":
        print("  The conflict is the owner's to resolve; it is recorded for the next session.")
    return EXIT.get(status, 1)


def verify(pid: str) -> int:
    """A parent card's acceptance (the whole outcome) on BASE, once every child has landed.

    Each card's checks run in its own worktree, and nothing used to check the merged whole: the only
    git hook judges branch rules, not behaviour. Pass: the parent is done. Fail: it is not, and the
    board names the failing command; the fix is a new child card."""
    try:
        card = cardmod.load(pid)
    except Exception as e:
        sys.exit(f"{e}")
    waiting = [k for k in card.get("children") or [] if not merged(k)]
    refuse = (f"{pid} is not a parent card (seat.py land checks one card)" if not cardmod.is_parent(card)
              else f"{pid} has no acceptance: nothing proves the whole outcome" if not card.get("acceptance")
              else f"{', '.join(waiting)} not merged into {BASE} yet" if waiting else "")
    if refuse:
        print(f"  cannot verify {pid}: {refuse}. Nothing was run.")
        return 1
    scratch = REPO.parent / f"{REPO.name}-verify-{pid.lower()}"
    _drop(scratch)
    r = git("worktree", "add", "--detach", str(scratch), BASE)
    if r.returncode != 0:
        sys.exit(f"could not create the scratch worktree {scratch}:\n{r.stderr.strip()}")
    try:
        acc, reason = checks_on(scratch, card)
    finally:
        _drop(scratch)
    passed, ts = sum(1 for a in acc if a["rc"] == 0), now_iso()[:16]
    if reason:
        board.set_state(pid, "blocked", f"verify {ts}Z: failed | {reason}"[:300])
        print(f"  VERIFY FAILED {pid}: {reason}\n  The parent is not done. Write a child card that fixes it.")
        return 1
    board.set_state(pid, "done", f"verify {ts}Z: whole outcome {passed}/{len(acc)} on {BASE}")
    print(f"  VERIFIED {pid}: whole outcome {passed}/{len(acc)} on {BASE}. The parent is done.")
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) >= 2 and a[0] == "clean":
        sys.exit(clean(a[1], "--abandon" in a))
    print(__doc__)
