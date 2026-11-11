#!/usr/bin/env python3
"""cto setup: the choices a project makes once, after install, and the workspace to start in.

    cto setup                         ask, with what is configured now as each default
    cto setup --yes                   take every default (scripts, CI)
    cto setup --answers file.json     take these answers; the rest as above
    cto setup ... --verify-models     one short call per chosen model before anything is written
    cto setup ... --no-doctor         do not run `cto --doctor` afterwards

WHY: install.sh asks nothing. Memory was configured by no step at all, so `memslice.py attach`
refused (scoping is mandatory) and a new owner learned that only when the CTO first tried; the
seats' models sat in four spec files nobody was told to open. This is the one place to choose.

It asks, in this order: the tool and plan (Claude Code, how it is paid for), memory, the team,
the scope, what may be spent; then it writes config.json and sets up the workspace: it offers to
clean up merged branches and stale worktrees, commits the install, and creates the integration
branch and its checkout, which `cto` needs to start (issue #18: all of that was done by hand).

Answers file (every key optional):
    {"billing": "subscription" | "api", "cto_effort": "default" | "low" | "medium" | "high" | "xhigh" | "max",
     "daily_usd": 20, "card_usd": 1,
     "workspace": {"commit_install": true, "create_integration": true,        (the defaults)
                   "prune_worktrees": false, "remove_merged_worktrees": false, "delete_merged_branches": false},
         (nothing is ever deleted unless one of the last three is true: --yes does not make them true)
     "memory": "both" | "claude-mem" | "mempalace" | "none",
     "mempalace_wing": "<wing>", "claude_mem_projects": ["<name>", ...],
     "cto_model": "<model id or default>",
     "seats": {"backend": {"keep": false}, "ui-developer": {"model": "<id>"}},
         (a seat by the `name:` in its spec, not its file name; an unknown one refuses, changing nothing)
     "seat_models": {"mechanical": "<id>", "standard": "<id>", "hard": "<id>"},
     "production_branch": "main", "integration_branch": "development",
     "guarded_paths": ["src/"], "delegation": "block" | "ask" | "allow",
     "integration_checks": ["npm test"]}

The owner's, never an agent's: it rewrites config.json and the seat specs, which the guard
otherwise asks the owner about. So it refuses to run inside an agent session.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _config  # noqa: E402

AI = HERE.parent                       # .ai-teammates
CONFIG = AI / "config.json"
AGENTS = AI / "agents"
MEMORIES = ("both", "claude-mem", "mempalace", "none")
BILLING = ("subscription", "api")
EFFORTS = ("default", "low", "medium", "high", "xhigh", "max")
HARNESS = "claude-code"     # the only one: hooks, which stop a seat during a run, fire nowhere else


class Asker:
    def __init__(self, answers: dict, interactive: bool):
        self.answers, self.interactive = answers, interactive

    def __call__(self, key: str, prompt: str, default, choices=None):
        """The answer for key (dotted path into the answers file), else ask, else the default."""
        node = self.answers
        for part in key.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        if node is not None:
            if isinstance(node, bool):
                node = "yes" if node else "no"
            if choices and node not in choices:
                sys.exit(f"  setup: {key} = {node!r} is not one of {', '.join(choices)}")
            return node
        if not self.interactive:
            return default
        shown = ", ".join(default) if isinstance(default, list) else default
        while True:
            got = input(f"  {prompt}" + (f" ({'/'.join(choices)})" if choices else "") + f" [{shown}]: ").strip()
            if not got:
                return default
            if isinstance(default, list):
                return [x.strip() for x in got.split(",") if x.strip()]
            if not choices or got in choices:
                return got
            print(f"    one of: {', '.join(choices)}")


def yes(v) -> bool:
    return v is True or str(v).lower() in ("y", "yes", "true", "keep")


def git(*a) -> str:
    return subprocess.run([_config.GIT, *a], cwd=str(AI.parent), capture_output=True, text=True).stdout


def gitp(*a, cwd=None):
    return subprocess.run([_config.GIT, *a], cwd=str(cwd or AI.parent), capture_output=True, text=True)


def as_number(got, low: float, high: float = None):
    """got as a float inside [low, high], or None."""
    try:
        v = float(got)
    except (TypeError, ValueError):
        return None
    if isinstance(got, bool) or v != v or v < low or (high is not None and v > high):
        return None
    return v


def number(ask, key: str, prompt: str, default: float, low: float, high: float = None) -> float:
    """A dollar amount: from the answers (checked by bad_answers before anything is written), or
    asked until it is one. A budget typed wrong must not silently become the default."""
    while True:
        v = as_number(ask(key, prompt, f"{default:g}"), low, high)
        if v is not None:
            return v
        if not ask.interactive:
            return default
        print(f"    a number from {low:g}" + (f" to {high:g}" if high is not None else " up"))


def bad_answers(answers: dict) -> str:
    """Why an answer about the plan or the spend cannot be right, or "". Checked before anything
    is written: setup changes seat specs as it goes, and "nothing was changed" has to be true."""
    import card as cardmod
    top = cardmod.limits()["max_usd"]
    for key, choices in (("billing", BILLING), ("cto_effort", EFFORTS)):
        if key in answers and answers[key] not in choices:
            return f"  setup: {key} = {answers[key]!r} is not one of {', '.join(choices)}; nothing was changed"
    if "daily_usd" in answers and as_number(answers["daily_usd"], 0) is None:
        return (f"  setup: daily_usd = {answers['daily_usd']!r} must be a number of 0 or more "
                f"(dollars a day for all card runs; 0 = no daily budget); nothing was changed")
    if "card_usd" in answers and as_number(answers["card_usd"], 0.01, top) is None:
        return (f"  setup: card_usd = {answers['card_usd']!r} must be a number from 0.01 to {top:g}: the most one "
                f"card run may cost is cards.max_usd in config.json; nothing was changed")
    return ""


# --- tools and plan ------------------------------------------------------------------------

def step_tools(ask, cfg: dict) -> list:
    """Which coding tool runs the team, and how it is paid for. One tool: Claude Code, because
    its hooks are the guards (docs/04-ROUTING.md). What is left to say is whether it is there and
    signed in, and whether a dollar figure in a report is money or an estimate."""
    print("\n── tools and plan ── what runs the CTO and the seats (docs/04-ROUTING.md)")
    claude = os.environ.get("AI_TEAMMATES_CLAUDE_BIN", "claude")
    todo = []
    print("  coding tool   Claude Code (the only one supported: its hooks are what stop a seat during a run)")
    if not shutil.which(claude):
        print("  claude        NOT installed")
        todo.append("install Claude Code: https://claude.com/claude-code")
    else:
        try:
            out = subprocess.run([claude, "auth", "status"], capture_output=True, text=True, timeout=30).stdout
            signed = bool(json.loads(out).get("loggedIn"))
        except Exception:
            signed = False
        print(f"  claude        {'signed in' if signed else 'NOT signed in'}")
        if not signed:
            todo.append("claude auth login")
    cto = dict(cfg.get("cto") or {})
    cto["billing"] = ask("billing", "How Claude Code is paid for here: a subscription (Pro, Max, Team) or API billing",
                         cto.get("billing") if cto.get("billing") in BILLING else "subscription", BILLING)
    print("                " + ("costs in reports are Claude Code's estimates: nothing is billed, but they are "
                                "what counts against your plan's limit" if cto["billing"] == "subscription"
                                else "costs in reports are money billed to your API account"))
    cfg["cto"] = cto
    return todo


def step_spend(ask, cfg: dict) -> None:
    import card as cardmod
    print("\n── spend ── what card runs may cost (docs/04-ROUTING.md)")
    top = cardmod.limits()["max_usd"]
    daily = number(ask, "daily_usd", "Daily budget for all card runs together, in dollars (0 = none)",
                   _config.daily_budget(), 0)
    cfg["budget"] = {**(cfg.get("budget") or {}), "daily_usd": daily}
    cap = number(ask, "card_usd", f"What one card run may cost when the card does not say, in dollars (at most {top:g})",
                 cardmod.limits()["default_usd"], 0.01, top)
    cards = dict(cfg.get("cards") or {})
    # only when it differs from the built-in default, so an upgrade's newer default still reaches this project
    if cap != cardmod.LIMITS["default_usd"]:
        cards["default_usd"] = cap
    else:
        cards.pop("default_usd", None)
    if cards:
        cfg["cards"] = cards
    else:
        cfg.pop("cards", None)


# --- memory --------------------------------------------------------------------------------

def memory_installed() -> dict:
    palace = Path(os.path.expanduser((_config.load().get("memory") or {}).get("mempalace_palace")
                                     or "~/.mempalace/palace"))
    return {"claude-mem": (Path.home() / ".claude-mem" / "settings.json").exists(),
            "mempalace": bool(shutil.which("mempalace")) and palace.exists()}


def claude_mem_names(project: str) -> list:
    """claude-mem names a project by its folder, and a worktree as "<main folder>/<worktree folder>"."""
    main = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir").strip() or AI.parent).parent
    names = [main.name or project]
    for line in git("worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree ") and Path(line[9:]).name != main.name:
            names.append(f"{main.name}/{Path(line[9:]).name}")
    return names


def step_memory(ask, cfg: dict, project: str) -> list:
    print("\n── memory ── what the CTO hands a seat on its card (docs/06-MEMORY.md)")
    mem = dict(cfg.get("memory") or {})
    have = memory_installed()
    for k, v in have.items():
        print(f"  {k:<11} {'installed' if v else 'not installed'}")
    now = ("both" if mem.get("mempalace_wing") and mem.get("claude_mem_projects") else "mempalace"
           if mem.get("mempalace_wing") else "claude-mem" if mem.get("claude_mem_projects") else "both")
    choice = ask("memory", "Memory systems", now, MEMORIES)
    for k in ("default", "mempalace_wing", "claude_mem_projects"):
        mem.pop(k, None)
    todo = []
    if choice in ("both", "mempalace"):
        mem["mempalace_wing"] = ask("mempalace_wing", "MemPalace wing for this project",
                                    (cfg.get("memory") or {}).get("mempalace_wing") or project)
        if not have["mempalace"]:
            todo += ["uv tool install mempalace        (or: pipx install mempalace)",
                     f"mempalace init {AI.parent}"]
    if choice in ("both", "claude-mem"):
        mem["claude_mem_projects"] = ask("claude_mem_projects", "claude-mem project names (comma-separated)",
                                         (cfg.get("memory") or {}).get("claude_mem_projects") or claude_mem_names(project))
        if not have["claude-mem"]:
            todo.append("npx claude-mem install")
    if choice != "none":
        # MemPalace first when there is a choice: it found the answer at rank 1 for 82% of questions
        # where claude-mem found 0 of 11 in its top 5 (tools/memory-bench/results/2026-10-02-pilot.md)
        mem["default"] = "claude-mem" if choice == "claude-mem" else "mempalace"
    if mem:
        cfg["memory"] = mem
    else:
        cfg.pop("memory", None)
    return todo


# --- team ----------------------------------------------------------------------------------

def frontmatter_set(path: Path, key: str, value: str) -> None:
    """Change one frontmatter key in a seat spec; a launch line's --model follows the model."""
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    head = m.group(1)
    old = re.search(rf"^{key}:\s*(.*)$", head, re.M)
    if old and key == "model":
        head = re.sub(rf"--model {re.escape(old.group(1).strip())}\b", f"--model {value}", head)
    head = re.sub(rf"^{key}:.*$", f"{key}: {value}", head, flags=re.M) if old else head + f"\n{key}: {value}"
    path.write_text(f"---\n{head}\n---\n" + text[m.end():], encoding="utf-8")


def step_team(ask, cfg: dict) -> tuple:
    import cardrun
    import seat
    print("\n── team ── the CTO and the seats it delegates to (docs/03-TEAM.md, docs/04-ROUTING.md)")
    cto = dict(cfg.get("cto") or {})
    cto["model"] = ask("cto_model", "CTO model (Claude Code; 'default' = the CLI's own)", cto.get("model") or "default")
    effort = ask("cto_effort", "CTO effort ('default' = the CLI's own; higher thinks longer and costs more)",
                 cto.get("effort") if cto.get("effort") in EFFORTS else "default", EFFORTS)
    if effort != "default":
        cto["effort"] = effort
    else:
        cto.pop("effort", None)
    cfg["cto"] = cto
    models, dropped = {cto["model"]} - {"default"}, []
    for name, spec in seat.load_agents().items():
        if spec.get("orchestrator") == "true":
            continue
        path = AI.parent / spec["_path"]
        desc = f"{spec.get('seat', name)}: {spec.get('harness', '?')}, {spec.get('model', '?')}"
        if not yes(ask(f"seats.{name}.keep", f"Keep seat {name} ({desc})?", "yes", ("yes", "no"))):
            (AGENTS / "off").mkdir(exist_ok=True)
            path.rename(AGENTS / "off" / path.name)      # install.sh does not bring it back
            dropped.append(name)
            continue
        model = ask(f"seats.{name}.model", f"  {name}: model", spec.get("model") or "")
        if model and model != spec.get("model"):
            frontmatter_set(path, "model", model)
        models.add(model)
    print("  card tiers: which model a card of each tier runs on (a card may name its own)")
    tiers = {**cardrun.DEFAULT_MODELS, **(cfg.get("seat_models") or {})}
    for t in ("mechanical", "standard", "hard"):
        tiers[t] = ask(f"seat_models.{t}", f"  tier {t}", tiers[t])
        models.add(tiers[t])
    # only what differs from the built-in defaults, so an upgrade's newer defaults still reach this project
    changed = {t: m for t, m in tiers.items() if cardrun.DEFAULT_MODELS.get(t) != m}
    if changed:
        cfg["seat_models"] = changed
    else:
        cfg.pop("seat_models", None)
    return sorted(m for m in models if m), dropped


def unknown_seats(answers: dict) -> str:
    """Why the answers name a seat that is not there, or "" when they do not.

    FOUND 2026-10-29: `"seats": {"backend": {"keep": false}}` in a project whose backend.md is named
    backend-<db> matched no seat and was skipped without a word, so the owner believed a seat dropped
    that still ran. Checked before anything is written: a seat is chosen by its spec's `name:`."""
    import seat
    named = sorted(n for n, s in seat.load_agents().items() if s.get("orchestrator") != "true")
    seats = answers.get("seats") or {}
    wrong = sorted(set(seats) - set(named)) if isinstance(seats, dict) else ["(seats is not an object)"]
    if not wrong:
        return ""
    files = {Path(s["_path"]).stem: n for n, s in seat.load_agents().items() if n in named}
    hints = "".join(f"\n    {w}.md is the seat named {files[w]}" for w in wrong if w in files)
    return (f"  setup: no seat named {', '.join(wrong)}; nothing was changed. The seats here: {', '.join(named)}"
            f" (a seat is chosen by the `name:` in its spec){hints}")


def unsupported_harness(answers: dict) -> str:
    """Why the answers ask for a harness that is not Claude Code, or "" when they do not.

    Until 2026-11 setup offered opencode and codex here and wrote the choice into the seat's spec,
    months after cards stopped running anywhere but Claude Code: the seat then ran nothing. Refused
    before anything is written, like a seat that is not there."""
    seats = answers.get("seats") or {}
    wrong = sorted(f"{n}: {v.get('harness')}" for n, v in seats.items()
                   if isinstance(v, dict) and v.get("harness") not in (None, HARNESS)) if isinstance(seats, dict) else []
    if not wrong:
        return ""
    return (f"  setup: seats run on {HARNESS} only ({', '.join(wrong)}); nothing was changed. Its hooks are what "
            f"stop a seat during a run (docs/04-ROUTING.md). Take \"harness\" out of the answers.")


def verify_models(models: list) -> list:
    """The models that do not answer. A model id that never existed sat in the routing doc for weeks."""
    claude = os.environ.get("AI_TEAMMATES_CLAUDE_BIN", "claude")
    bad = []
    for m in models:
        try:
            r = subprocess.run([claude, "--model", m, "-p", "Reply with the word ok", "--max-turns", "1"],
                               capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
            okay = r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            okay = False
        print(f"  {'ok  ' if okay else 'FAIL'}  {m}")
        if not okay:
            bad.append(m)
    return bad


# --- scope ---------------------------------------------------------------------------------

def step_scope(ask, cfg: dict) -> None:
    print("\n── scope ── what is product code, and how work reaches production (docs/02-GUARDRAILS.md)")
    prod, integ = _config.branches()
    prod = ask("production_branch", "Production branch", prod)
    integ = ask("integration_branch", "Integration branch", integ or "development")
    cfg["protected_branches"] = [prod, integ]
    cfg["guarded_paths"] = ask("guarded_paths", "Product code folders (comma-separated)",
                               list(cfg.get("guarded_paths") or _config.guarded()))
    # block is the default and is written down: tasks reach the project through cards, and the
    # owner sees the rule in config.json instead of having to know what an absent key means.
    had = (cfg.get("delegation") or {}).get("cto_product_edits")
    pol = ask("delegation", "When the CTO itself edits product code (block = it must write a card)",
              had if had in ("block", "ask", "allow") else "block", ("block", "ask", "allow"))
    cfg["delegation"] = {**(cfg.get("delegation") or {}), "cto_product_edits": pol}
    checks = ask("integration_checks", "Checks run on every landed card (comma-separated commands, '-' for none)",
                 list(cfg.get("integration_checks") or []))
    checks = [c for c in checks if c != "-"]
    if checks:
        cfg["integration_checks"] = checks
    else:
        cfg.pop("integration_checks", None)


# --- workspace -----------------------------------------------------------------------------

INSTALL_PATHS = (".ai-teammates", ".claude/settings.json")


def worktrees() -> list:
    """[{path, branch, prunable}] for every worktree git knows, the main checkout first."""
    out, cur = [], None
    for line in git("worktree", "list", "--porcelain").splitlines() + [""]:
        if line.startswith("worktree "):
            cur = {"path": Path(line[9:]), "branch": "", "prunable": False}
        elif line.startswith("branch refs/heads/") and cur:
            cur["branch"] = line[len("branch refs/heads/"):]
        elif line.startswith("prunable") and cur:
            cur["prunable"] = True
        elif not line and cur:
            out.append(cur)
            cur = None
    return out


def merged_into(branch: str, prod: str) -> bool:
    """Nothing on `branch` that `prod` does not have: `git log prod..branch` is empty."""
    r = gitp("rev-list", "--count", f"{prod}..{branch}")
    return r.returncode == 0 and r.stdout.strip() == "0"


def step_workspace(ask, prod: str, integ: str, project: str) -> None:
    """Leave the project ready for `cto`: the install committed, and the integration branch
    checked out beside the project. And offer to clean up, since a new owner often points this at
    a repository with a year of leftovers.

    Deleting is only ever: a branch with NOTHING the production branch lacks, not checked out
    anywhere, tagged archive/<branch>-<time> first (so `git branch <name> <tag>` brings it back);
    a worktree with no uncommitted file whose branch is merged; a worktree record whose folder
    is already gone. Each only on a yes of its own, and --yes is not that yes. Never the
    production or integration branch, never the branch you are on, never anything on a remote."""
    print("\n── workspace ── where the CTO works (docs/01-WORKFLOW.md)")
    if gitp("rev-parse", "--verify", "--quiet", "HEAD").returncode != 0 \
            or gitp("rev-parse", "--verify", "--quiet", f"refs/heads/{prod}").returncode != 0:
        print(f"  there is no commit on '{prod}' yet, so no branch or checkout can be made from it.\n"
              f"  Make a first commit, then run `cto setup` again.")
        return
    here = Path(git("rev-parse", "--show-toplevel").strip() or AI.parent).resolve()
    current = git("rev-parse", "--abbrev-ref", "HEAD").strip()
    keep = {prod, integ, current}

    # 1. what could be cleaned, and only on a yes
    wts = worktrees()
    stale = [w for w in wts if w["prunable"] or not w["path"].exists()]
    idle = [w for w in wts[1:] if w not in stale and w["branch"] and w["branch"] not in keep
            and w["path"].resolve() != here and merged_into(w["branch"], prod)
            and not gitp("status", "--porcelain", cwd=w["path"]).stdout.strip()]
    if stale:
        print(f"  {len(stale)} worktree record(s) whose folder is gone: " + ", ".join(w["path"].name for w in stale))
        if yes(ask("workspace.prune_worktrees", "Remove those records (git worktree prune)?", "no", ("yes", "no"))):
            gitp("worktree", "prune")
            print("  pruned")
    if idle:
        print(f"  {len(idle)} worktree(s) with no uncommitted file, on a branch already merged into {prod}: "
              + ", ".join(f"{w['path'].name} ({w['branch']})" for w in idle))
        if yes(ask("workspace.remove_merged_worktrees", "Remove those worktrees?", "no", ("yes", "no"))):
            for w in idle:
                r = gitp("worktree", "remove", str(w["path"]))       # no --force: git refuses a dirty one
                print(f"  {'removed' if r.returncode == 0 else 'kept (' + r.stderr.strip()[:80] + ')'}  {w['path'].name}")
    busy = {w["branch"] for w in worktrees() if w["branch"]}
    names = [b for b in git("for-each-ref", "--format=%(refname:short)", "refs/heads").split()
             if b not in keep and merged_into(b, prod)]
    dead = [b for b in names if b not in busy]
    if names:
        print(f"  {len(names)} branch(es) with nothing that {prod} lacks: " + ", ".join(names)
              + (f"  ({len(names) - len(dead)} checked out in a worktree: kept)" if len(dead) != len(names) else ""))
    if dead and yes(ask("workspace.delete_merged_branches",
                        f"Delete the {len(dead)} that are not checked out (each tagged archive/... first)?",
                        "no", ("yes", "no"))):
        import time
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for b in dead:
            tag = f"archive/{b}-{stamp}"
            if gitp("tag", tag, b).returncode != 0 or not merged_into(b, prod):
                print(f"  kept     {b} (could not tag it)")
                continue
            r = gitp("branch", "-D", b)
            print(f"  {'deleted ' if r.returncode == 0 else 'kept    '} {b}" + (f"  (restore: git branch {b} {tag})"
                                                                               if r.returncode == 0 else ""))

    # 2. the install, committed: a worktree is born with what is committed and nothing else
    paths = [p for p in INSTALL_PATHS if (here / p).exists()]
    if paths and gitp("status", "--porcelain", "--", *paths).stdout.strip():
        where = f"on {current}" + ("" if current == prod else f" (not {prod}: new worktrees get the guards only "
                                                              f"once this reaches {prod})")
        if yes(ask("workspace.commit_install", f"Commit the AI-teammates files {where} now?", "yes", ("yes", "no"))):
            gitp("add", "--", *paths)
            r = gitp("commit", "-q", "-m", "chore: install AI-teammates", "--", *paths)
            print("  committed: chore: install AI-teammates" if r.returncode == 0
                  else f"  NOT committed: {(r.stderr or r.stdout).strip()[:160]}")
        else:
            print(f"  not committed. Until it is, a new worktree starts unguarded:\n"
                  f"    git add {' '.join(paths)} && git commit -m 'chore: install AI-teammates'")

    # 3. the integration branch and its checkout: `cto` works there, never on production
    if not integ:
        print("  no integration branch is configured: `cto` works on feature branches off " + prod)
        return
    has_branch = gitp("rev-parse", "--verify", "--quiet", f"refs/heads/{integ}").returncode == 0
    at = next((w["path"] for w in worktrees() if w["branch"] == integ), None)
    if at:
        print(f"  {integ} is checked out at {at}")
    elif yes(ask("workspace.create_integration",
                 f"Create {'the checkout of' if has_branch else 'the branch'} '{integ}' "
                 f"{'' if has_branch else f'from {prod} and its checkout '}at ../{project}-dev?", "yes", ("yes", "no"))):
        dest = here.parent / f"{project}-dev"
        if dest.exists():
            print(f"  {dest} already exists and is not a checkout of {integ}: nothing was created. Pick a folder:\n"
                  f"    git worktree add <folder> {integ}")
            return
        if not has_branch:
            gitp("branch", integ, prod)
        r = gitp("worktree", "add", str(dest), integ)
        if r.returncode != 0:
            print(f"  could not create the checkout: {r.stderr.strip()[:200]}")
            return
        at = dest
        print(f"  created {integ} at {dest}")
    if at:
        print(f"\n  Start here:  cd {at} && cto")


def main(argv) -> int:
    if os.environ.get("CLAUDE_AGENT_NAME") or os.environ.get("AI_TEAMMATES_SEAT") or os.environ.get("AI_TEAMMATES_CARD"):
        print("  setup is the owner's: it rewrites config.json and the seat specs, which an agent may not "
              "change without asking. Run `cto setup` in your own terminal.")
        return 2
    answers = {}
    if "--answers" in argv:
        i = argv.index("--answers")
        try:
            answers = json.loads(Path(argv[i + 1]).read_text(encoding="utf-8"))
        except (IndexError, OSError, ValueError) as e:
            print(f"  setup: cannot read the answers file ({type(e).__name__}: {e})")
            return 1
    for problem in (unknown_seats(answers), unsupported_harness(answers), bad_answers(answers)):
        if problem:
            print(problem)
            return 1
    ask = Asker(answers, ("--ask" in argv or sys.stdin.isatty()) and "--yes" not in argv)
    cfg = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    before = json.dumps(cfg, sort_keys=True)
    project = cfg.get("project") or AI.parent.name

    todo = step_tools(ask, cfg)
    todo += step_memory(ask, cfg, project)
    models, dropped = step_team(ask, cfg)
    step_scope(ask, cfg)
    step_spend(ask, cfg)

    if "--verify-models" in argv or (ask.interactive and models
                                     and yes(ask("verify_models", "Check each model answers now (one short call each)?",
                                                 "no", ("yes", "no")))):
        print("\n── models ──")
        bad = verify_models(models)
        if bad:
            print(f"\n  {len(bad)} model(s) did not answer: {', '.join(bad)}. config.json was not changed; "
                  "fix the id (claude --model <id> -p hi) and run setup again.")
            return 1

    if json.dumps(cfg, sort_keys=True) != before:
        CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\n── done ──\n  wrote {CONFIG.relative_to(AI.parent)}" + (f"; seats dropped: {', '.join(dropped)} "
          f"(kept in {AGENTS.relative_to(AI.parent)}/off/)" if dropped else ""))
    if todo:
        print("  still to do, by you:\n" + "\n".join(f"    {t}" for t in todo))
    prod, integ = cfg["protected_branches"][0], (cfg["protected_branches"] + [""])[1]
    step_workspace(ask, prod, integ, project)
    print("\n  On a codebase the CTO has not seen, ask it for the first-session survey\n"
          "  (docs/08-NEW-PROJECT.md): it proposes the team and scope from the code itself.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
