#!/usr/bin/env python3
"""cto setup: the choices a project makes once, after install: memory, team, scope.

    cto setup                         ask, with what is configured now as each default
    cto setup --yes                   take every default (scripts, CI)
    cto setup --answers file.json     take these answers; the rest as above
    cto setup ... --verify-models     one short call per chosen model before anything is written
    cto setup ... --no-doctor         do not run `cto --doctor` afterwards

WHY: install.sh asks nothing. Memory was configured by no step at all, so `memslice.py attach`
refused (scoping is mandatory) and a new owner learned that only when the CTO first tried; the
seats' models sat in four spec files nobody was told to open. This is the one place to choose.

Answers file (every key optional):
    {"memory": "both" | "claude-mem" | "mempalace" | "none",
     "mempalace_wing": "<wing>", "claude_mem_projects": ["<name>", ...],
     "cto_model": "<model id or default>",
     "seats": {"backend": {"keep": false}, "ui-developer": {"model": "<id>", "harness": "claude-code"}},
         (a seat by the `name:` in its spec, not its file name; an unknown one refuses, changing nothing)
     "seat_models": {"mechanical": "<id>", "standard": "<id>", "hard": "<id>"},
     "production_branch": "main", "integration_branch": "development",
     "guarded_paths": ["src/"], "delegation": "allow" | "ask" | "block",
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
HARNESSES = ("claude-code", "opencode", "codex")


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
        harness = ask(f"seats.{name}.harness", f"  {name}: harness", spec.get("harness") or "claude-code", HARNESSES)
        model = ask(f"seats.{name}.model", f"  {name}: model", spec.get("model") or "")
        if harness != spec.get("harness"):
            frontmatter_set(path, "harness", harness)
            if harness != "claude-code":
                print(f"    note: cards run only on Claude Code seats; {name} now runs free-form tasks only, "
                      f"and its spec needs a launch/headless command for {harness} (docs/04-ROUTING.md)")
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
    had = (cfg.get("delegation") or {}).get("cto_product_edits")
    pol = ask("delegation", "When the CTO itself edits product code", had or "allow", ("allow", "ask", "block"))
    if pol != "allow" or had:
        cfg["delegation"] = {**(cfg.get("delegation") or {}), "cto_product_edits": pol}
    checks = ask("integration_checks", "Checks run on every landed card (comma-separated commands, '-' for none)",
                 list(cfg.get("integration_checks") or []))
    checks = [c for c in checks if c != "-"]
    if checks:
        cfg["integration_checks"] = checks
    else:
        cfg.pop("integration_checks", None)


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
    unknown = unknown_seats(answers)
    if unknown:
        print(unknown)
        return 1
    ask = Asker(answers, ("--ask" in argv or sys.stdin.isatty()) and "--yes" not in argv)
    cfg = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    before = json.dumps(cfg, sort_keys=True)
    project = cfg.get("project") or AI.parent.name

    todo = step_memory(ask, cfg, project)
    models, dropped = step_team(ask, cfg)
    step_scope(ask, cfg)

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
        print("  memory not installed yet, run:\n" + "\n".join(f"    {t}" for t in todo))
    print("  commit .ai-teammates/ so every worktree sees it, then start `cto` and ask for the first-session\n"
          "  survey (docs/08-NEW-PROJECT.md): it proposes the team and scope from the code itself.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
