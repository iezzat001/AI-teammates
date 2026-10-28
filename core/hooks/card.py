#!/usr/bin/env python3
"""Task cards: the only thing a seat ever sees.

WHY (measured on ContentOS, 2026-10-02). Eight seat runs in a month, five of 94
board cards delegated. The delegated ones were feature-sized ("read SCOUT_SPEC.md
and build /scout", 15-25 minutes), board cards were post-mortems written after
the work (median note 348 characters), and nothing linked a card to a run. The
one build that went well had a 108-line brief with the schema, file:line
pointers, numbered steps and checkable done-criteria. That is a ticket. This
module makes the ticket the unit of delegation.

A card is the Jira ticket a junior could pick up cold: one goal, the files they
may touch, the steps, the context the CTO chose for them, and commands that
prove it is done. A seat given a card does not see the board, the docs or the
architecture, and cannot go looking: that is the point.

    card.py new <ID> --seat S --tier T --title "..." --goal "..."
    card.py check <ID>        validate, and say how to split it if it is too big
    card.py show <ID>         the exact prompt the seat will receive
    card.py accept [ID]       run the card's acceptance commands here (the seat's self-check)
    card.py ls
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _config  # noqa: E402

CARDS = Path(__file__).resolve().parents[1] / "cards"
TIERS = ("mechanical", "standard", "hard")
LIMITS = {
    "max_edit_files": 5,        # more than this is two cards
    "max_goal_chars": 240,
    "max_steps": 12,
    "max_memory_chars": 3200,   # ~800 tokens: a slice, not a dump
    "max_minutes": 30,
    "max_tool_calls": 60,
    "default_minutes": 15,
    "default_tool_calls": 40,
    "idle_minutes": 10,
    "acceptance_timeout": 300,
}
# A seat never edits the rails it runs inside.
FORBIDDEN_EDIT = (".ai-teammates/", ".claude/", ".git/")
SECRETISH = re.compile(r"(^|/)(\.env[^/]*|[^/]*(secret|credential)[^/]*|id_rsa[^/]*)$", re.I)


def limits() -> dict:
    out = dict(LIMITS)
    out.update(_config.load().get("cards") or {})
    return out


def path(cid: str) -> Path:
    return CARDS / f"{cid}.json"


def load(cid: str) -> dict:
    p = path(cid)
    if not p.exists():
        raise FileNotFoundError(f"no card {cid} ({p})")
    return json.loads(p.read_text(encoding="utf-8"))


def save(card: dict) -> Path:
    CARDS.mkdir(parents=True, exist_ok=True)
    p = path(card["id"])
    p.write_text(json.dumps(card, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


def budget(card: dict) -> dict:
    L, b = limits(), card.get("budget") or {}
    return {"minutes": b.get("minutes", L["default_minutes"]),
            "tool_calls": b.get("tool_calls", L["default_tool_calls"])}


def edit_scope(card: dict) -> list:
    return list((card.get("scope") or {}).get("edit") or [])


def allowed_edit(card: dict, rel: str) -> bool:
    """A file path is in scope when it is listed, or sits under a listed directory."""
    for e in edit_scope(card):
        if rel == e or (e.endswith("/") and rel.startswith(e)):
            return True
    return False


# A card may grant its seat extra COMMANDS, never tools and never a shell: the seat's tools
# come from its spec, and cardrun appends these to --allowedTools verbatim. FOUND 2026-10-03 by
# the security-audit seat: nothing checked them, so `Bash(cat:*)` or `Bash` on a card would have
# let a seat write any file through the shell, past the live scope check (which watches edits).
ALLOW_RE = re.compile(r"^Bash\(([^()*;&|<>`$\\\n]+?)(:\*)?\)$")
UNSAFE_LEAD = {"sh", "bash", "zsh", "fish", "dash", "ksh", "env", "sudo", "su", "eval", "exec", "xargs",
               "command", "nohup", "python", "python3", "node", "deno", "bun", "npx", "pnpm", "yarn", "perl",
               "ruby", "php", "osascript", "awk", "sed", "find", "tee", "dd", "cp", "mv", "rm", "ln", "chmod",
               "chown", "truncate", "curl", "wget", "ssh", "scp", "rsync", "nc", "open"}
READ_ONLY_GIT = {"diff", "log", "show", "status", "grep", "blame", "ls-files", "rev-parse"}
NPM_NO = {"install", "i", "ci", "exec", "x", "publish", "uninstall", "add", "update", "link", "rebuild"}


def allow_problems(card: dict) -> list:
    out = []
    allow = card.get("allow") or []
    if not isinstance(allow, list):
        return ["allow must be a list of Bash(<command>) entries"]
    for a in allow:
        m = ALLOW_RE.match(a) if isinstance(a, str) else None
        words = m.group(1).split() if m else []
        lead = Path(words[0]).name if words else ""
        if not words:
            out.append(f"allow '{a}': grant one command, as Bash(<command>) or Bash(<command prefix>:*), "
                       "with no shell syntax. Tools come from the seat's spec, not from a card")
        elif lead in UNSAFE_LEAD:
            out.append(f"allow '{a}': '{words[0]}' can run or write anything. Name the exact command the seat needs")
        elif lead == "git" and (len(words) < 2 or words[1] not in READ_ONLY_GIT):
            out.append(f"allow '{a}': a seat gets read-only git only ({', '.join(sorted(READ_ONLY_GIT))}); "
                       "the runner commits for it")
        elif lead == "npm" and (len(words) < 2 or words[1] in NPM_NO):
            out.append(f"allow '{a}': npm may run a script (npm run <x>, npm test), not install or execute packages")
    return out


def validate(card: dict, expect_id: str = "") -> list:
    """Problems that make this card the wrong size or shape. [] = a seat can take it.

    The size limits are the point: a card that fails them is not a harder task, it
    is two tasks, and the junior-with-no-context model only works on one."""
    L, bad = limits(), []
    cid = str(card.get("id") or "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", cid):
        bad.append("id must be letters, digits, '-', '_' or '.'")
    if expect_id and cid != expect_id:
        bad.append(f"id '{cid}' does not match the file name '{expect_id}'")
    goal = str(card.get("goal") or "").strip()
    if not goal:
        bad.append("goal is empty: one sentence saying what is true when this is done")
    elif len(goal) > L["max_goal_chars"] or "\n" in goal:
        bad.append(f"goal is {len(goal)} chars (max {L['max_goal_chars']}, one line): "
                   "if it needs more words it is more than one task, so split the card")
    if not card.get("seat"):
        bad.append("seat is missing")
    if (card.get("tier") or "standard") not in TIERS:
        bad.append(f"tier must be one of {', '.join(TIERS)}")

    edits = edit_scope(card)
    if not edits and not card.get("read_only"):
        bad.append("scope.edit is empty: name the files this card may change")
    if len(edits) > L["max_edit_files"]:
        bad.append(f"scope.edit lists {len(edits)} files (max {L['max_edit_files']}): split the card "
                   "so each one changes a handful of files")
    for e in edits:
        if not isinstance(e, str) or not e.strip():
            bad.append("scope.edit has an empty entry")
        elif e.startswith("/") or ".." in Path(e).parts or "*" in e:
            bad.append(f"scope.edit '{e}': use repo-relative paths, no '..', no globs")
        elif e.startswith(FORBIDDEN_EDIT) or SECRETISH.search(e):
            bad.append(f"scope.edit '{e}' is not a file a seat may change")

    bad += allow_problems(card)

    steps = card.get("steps") or []
    if len(steps) > L["max_steps"]:
        bad.append(f"{len(steps)} steps (max {L['max_steps']}): split the card")

    acc = card.get("acceptance") or []
    if not acc and not card.get("read_only"):
        bad.append("acceptance is empty: give at least one command that proves it is done "
                   "(exit 0 = pass). 'Looks right' is not a check")
    for a in acc:
        cmd = str((a or {}).get("run") or "").strip()
        if not cmd or "\n" in cmd or len(cmd) > 300:
            bad.append("each acceptance needs a one-line shell command in 'run'")
        elif re.match(r"^[A-Z][a-z]+ [a-z]+ [a-z]+", cmd) and not re.search(r"[/.|&;$=-]", cmd):
            bad.append(f"acceptance '{cmd[:40]}...' reads like a sentence, not a command")

    mem = (card.get("memory") or {}).get("text") or ""
    if len(mem) > L["max_memory_chars"]:
        bad.append(f"memory slice is {len(mem)} chars (max {L['max_memory_chars']}): "
                   "keep only what this one task needs")
    b = card.get("budget") or {}
    if b.get("minutes", 0) > L["max_minutes"]:
        bad.append(f"budget over {L['max_minutes']} minutes: split the card")
    if b.get("tool_calls", 0) > L["max_tool_calls"]:
        bad.append(f"budget over {L['max_tool_calls']} tool calls: split the card")

    # The blank template must not be runnable: <path/to/file> is a valid-looking path.
    vals = [goal, *edits, *steps, *(card.get("out_of_scope") or []),
            *[str((a or {}).get("run") or "") for a in acc]]
    holes = [v for v in vals if isinstance(v, str) and v.strip().startswith("<") and v.strip().endswith(">")]
    if holes:
        bad.append(f"unfilled template placeholder: {holes[0][:50]}")
    return bad


def render(card: dict) -> str:
    """The whole world the seat gets."""
    edits, reads = edit_scope(card), list((card.get("scope") or {}).get("read") or [])
    b = budget(card)
    out = [
        "You are executing ONE task card. You know nothing about the wider project and "
        "you do not need to: everything you need is on this card.",
        "",
        f"CARD {card['id']}: {card.get('title') or card['goal'][:60]}",
        f"GOAL: {card['goal']}",
        "",
        "YOU MAY CHANGE ONLY: " + (", ".join(edits) if edits else "(nothing: read-only)"),
    ]
    if reads:
        out.append("YOU MAY ALSO READ: " + ", ".join(reads))
    if card.get("steps"):
        out += ["", "STEPS:"] + [f"  {i}. {s}" for i, s in enumerate(card["steps"], 1)]
    mem = (card.get("memory") or {}).get("text")
    if mem:
        out += ["", "BACKGROUND FACTS (chosen for this task by the CTO; do not search for more):",
                "<<<", mem.strip(), ">>>"]
    acc = card.get("acceptance") or []
    if acc:
        # What must be TRUE, in words, not the shell. FOUND on the DOC-2 pilot (twice): a seat
        # shown the raw commands pastes them into its Bash tool, however firmly it is told the
        # runner will run them. A junior needs the outcome and a button that runs the real checks.
        out += ["", "DONE WHEN all of this is true (the runner checks it after you stop; to check yourself first run "
               "`python3 .ai-teammates/hooks/card.py accept`, which runs the real checks for you; "
               "any other way of verifying is refused):"]
        out += [f"  - {a.get('why') or a['run']}" for a in acc]
    runnable = ([] if card.get("read_only") else ["python3 .ai-teammates/hooks/card.py accept",
                                                  "git rm <one of the files above> (only to delete it)"]) \
        + list(card.get("allow") or [])
    if runnable:
        out += ["", "COMMANDS YOU MAY RUN: " + ", ".join(runnable)]
    if card.get("out_of_scope"):
        out += ["", "NOT YOUR JOB:"] + [f"  - {s}" for s in card["out_of_scope"]]
    out += ["", "RULES",
            f"  - Budget: about {b['minutes']} minutes and {b['tool_calls']} tool calls. A small task should not need it.",
            "  - Edits outside the files above are denied, and so is any command not listed under COMMANDS YOU MAY RUN "
            "(none listed = none allowed). A refusal is not an error to fix: do not retry or rephrase it.",
            "  - Do not commit and do not push. Leave your changes in the working tree; the runner commits.",
            "  - If you hit something you cannot do within this card (an approval, a login, billing, "
            "a missing tool or credential, a file you need that is not listed), STOP. Do not work around it, "
            "do not try a second spelling. Your final message must then be exactly one line:",
            "        BLOCKED: <class> | <the one thing you need from the human>"]
    extra = card.get("stop_and_ask") or []
    if extra:
        out += ["  - Also stop and ask if: " + "; ".join(extra)]
    out += ["  - Otherwise your final message is three short lines: what you changed, what you verified "
            "(and how), what you could not verify."]
    return "\n".join(out)


def run_acceptance(card: dict, wt: Path) -> list:
    """Execute each acceptance command in the worktree. Exit 0 = pass. Executed, not
    asserted: the seat's own report that it is done is not evidence."""
    L, res = limits(), []
    for a in card.get("acceptance") or []:
        cmd = a["run"]
        try:
            p = subprocess.run(["/bin/sh", "-c", cmd], cwd=str(wt), capture_output=True, text=True,
                               timeout=L["acceptance_timeout"],
                               env={**__import__("os").environ, "PATH": "/usr/bin:/bin:" +
                                    __import__("os").environ.get("PATH", "")})
            tail = ((p.stdout or "") + (p.stderr or "")).strip()[-200:]
            res.append({"run": cmd, "rc": p.returncode, "tail": tail})
        except subprocess.TimeoutExpired:
            res.append({"run": cmd, "rc": 124, "tail": f"timed out after {L['acceptance_timeout']}s"})
        except Exception as e:
            res.append({"run": cmd, "rc": 127, "tail": f"{type(e).__name__}: {e}"})
    return res


def template(cid: str, seat: str, tier: str, title: str, goal: str) -> dict:
    return {
        "id": cid, "title": title, "goal": goal, "seat": seat, "tier": tier,
        "scope": {"edit": ["<path/to/file>"], "read": []},
        "steps": ["<the exact change, with file:line and the new text where you can>"],
        "memory": {"source": "", "query": "", "ids": [], "text": ""},
        "acceptance": [{"run": "<command that exits 0 when done>", "why": "<what it proves>"}],
        "allow": [],
        "budget": {"minutes": limits()["default_minutes"], "tool_calls": limits()["default_tool_calls"]},
        "out_of_scope": ["<what a helpful agent might be tempted to also do>"],
        "stop_and_ask": [],
    }


def main(argv) -> int:
    cmd = argv[0] if argv else "ls"
    if cmd == "ls":
        for p in sorted(CARDS.glob("*.json")) if CARDS.exists() else []:
            try:
                c = json.loads(p.read_text())
                print(f"  {c['id']:<10} {c.get('seat', '?'):<16} {c.get('tier', 'standard'):<10} {c.get('goal', '')[:60]}")
            except Exception:
                print(f"  {p.stem:<10} (unreadable)")
        return 0
    if cmd == "accept":
        # The seat's sanctioned self-check. A seat shown the DONE WHEN commands will try to run
        # them (the DOC-2 pilot, twice), and refusing that punished a good instinct. This runs
        # exactly the card's acceptance commands, in the seat's own worktree, via ONE allowed
        # command, so the seat can iterate instead of the runner finding out afterwards.
        try:
            c = load(argv[1]) if len(argv) > 1 else json.loads(Path(os.environ["AI_TEAMMATES_CARD"]).read_text())
        except Exception as e:
            print(f"  accept: no card to check ({type(e).__name__}: {e}). Pass an ID, or run it inside a card run.")
            return 2
        # acceptance may look at the staged diff, which the runner stages for it: do the same
        subprocess.run([_config.GIT, "add", "-A", "--", *edit_scope(c)], capture_output=True)
        results = run_acceptance(c, Path.cwd())
        for a in results:
            print(f"  {'PASS' if a['rc'] == 0 else 'FAIL'}  {a['run'][:100]}" + ("" if a['rc'] == 0 else f"\n        {a['tail'][-160:]}"))
        failed = sum(1 for a in results if a["rc"] != 0)
        print(f"  {len(results) - failed}/{len(results)} acceptance commands pass")
        return 0 if not failed else 1
    if cmd == "new" and len(argv) >= 2:
        opts = {argv[i]: argv[i + 1] for i in range(2, len(argv) - 1, 2) if argv[i].startswith("--")}
        if path(argv[1]).exists():
            print(f"  card {argv[1]} already exists: {path(argv[1])}")
            return 1
        c = template(argv[1], opts.get("--seat", "ui-developer"), opts.get("--tier", "standard"),
                     opts.get("--title", ""), opts.get("--goal", ""))
        print(f"  wrote {save(c)}\n  fill it in, then: card.py check {argv[1]}")
        try:
            import board
            if board.ensure(argv[1], c["seat"], c["title"] or c["goal"] or argv[1]):
                print(f"  added {argv[1]} to the board")
        except Exception:
            pass
        return 0
    if cmd in ("check", "show") and len(argv) >= 2:
        try:
            c = load(argv[1])
        except Exception as e:
            print(f"  {e}")
            return 1
        problems = validate(c, argv[1])
        if cmd == "show":
            print(render(c))
        if problems:
            print(f"\n  card {argv[1]} is NOT ready ({len(problems)} problem(s)):")
            for p in problems:
                print(f"    - {p}")
            return 1
        b = budget(c)
        print(f"  card {argv[1]} ok · {len(edit_scope(c))} file(s) in scope · {len(c.get('acceptance') or [])} "
              f"acceptance · tier {c.get('tier', 'standard')} · {b['minutes']} min / {b['tool_calls']} calls")
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
