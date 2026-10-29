# Start here

## Install

```bash
cd ~/your-project
/path/to/AI-teammates/install.sh
cto setup             # memory, team and scope, once; ends with cto --doctor
cto                   # start talking
```

`install.sh` writes `.ai-teammates/` into your project (hooks, agent specs,
docs, config, board) and puts `cto` and `ai-teammates` on your PATH. Outside
`.ai-teammates/` it also: registers its hooks in `.claude/settings.json`
(merging, never replacing yours), writes a deny-list into `opencode.json`, and
installs a git `pre-push` hook (never over one it did not write). Runtime state
lives in `.git/ai-teammates/`, shared by every worktree and never committed.

Re-running upgrades hooks, docs and `cto.md`. It never touches your config,
board, `RULES.md`, or seat specs you already have, and does not bring back a seat you dropped.

`cto setup` asks the questions the installer does not: which memory (claude-mem, MemPalace or
both, scoped to this project), which seats to keep and on which harness and model, the model per
card tier, the branches, what counts as product code, the delegation policy and the integration
checks. Each default is what is configured now, so re-running it changes only what you change.
`--yes` takes every default; `--answers file.json` scripts it (the keys are in `setup.py`). It is
yours, not an agent's: it refuses inside an agent session, and the guard asks you if one tries.

---

## First session

`cto` runs pre-flight before you can type:

```
── pre-flight ──────────────────────────
  no seat running
  STATIC — is each seat bound by the thing its harness actually reads?
  ok    ui-developer: 15 bash rules, all required denies present
  PREPUSH — does it still block product code on main?
  ok    prepush.py BLOCKED a product commit on main (executed, not grepped)
  all guardrails in force
── git ─────────────────────────────────
  on main · 0 dirty · 4 worktrees
```

Read the last line of each block. "executed, not grepped" is the point: the
check ran the guardrail against a real commit rather than reading its source and
assuming.

Then describe work in plain language. The CTO decides whether to do it or
delegate it, and tells you which. On a codebase it has not seen, start with the survey in
[08-NEW-PROJECT](08-NEW-PROJECT.md): it reads the code and proposes the team and scope.

---

## What to expect, honestly

**It will be slower than doing it yourself** for anything small. The value shows
up on work that spans several files, or that you would otherwise put off.

**It will be wrong sometimes.** The system is built so that wrongness surfaces —
guardrails that execute, a review protocol, reports that separate *verified*
from *assumed*. It is not built to prevent all error, which is not achievable.

**You are still the reviewer.** Production deploys need your word. So does
anything that spends money.

---

## Configure before real work

`.ai-teammates/config.json`:

```json
{
  "protected_branches": ["main", "development"],
  "guarded_paths": ["src/", "lib/", "app/"],
  "cto": { "provider": "anthropic", "model": "claude-opus-5-5", "effort": "max" }
}
```

**`guarded_paths` is the one to get right.** It is what `prepush` and `guard`
consider product code. Too narrow and unreviewed code reaches your main branch;
too broad and every doc edit needs a feature branch, which trains everyone to
override the guardrail — and an ignored guardrail is worse than none.

`install.sh` fills `protected_branches` from the branch your repository ships from and
`guarded_paths` from the source folders it finds; check both. The CTO runs on Claude Code's
default model until you name one; see [04-ROUTING](04-ROUTING.md) for the measured choice.
Verify a model id resolves before you configure it (`claude --model <id> -p hi`). Our own
routing doc once named a model that had never existed, for weeks.

**`integration_checks`** (optional): project commands, such as `["npm run typecheck", "npm test"]`,
that `seat.py land` runs on the merged result every time a card lands. Two cards can each pass
their own checks and break each other; this is where the second one is refused.

**`delegation`** (optional): `{"cto_product_edits": "allow" | "ask" | "block"}` decides what happens
when the CTO itself edits product code, in any checkout: `allow` (the default), `ask` you each time,
or `block` (product code arrives only through cards). `seat.py report --delegation` shows how much
product code reached the integration branch through cards and how much the CTO wrote.

---

## Then read

- [01-WORKFLOW](01-WORKFLOW.md) — branches, worktrees, the dashboard
- [02-GUARDRAILS](02-GUARDRAILS.md) — every rule and the incident behind it
- [07-FAILURE-CATALOGUE](07-FAILURE-CATALOGUE.md) — read this one even if you
  skip the others

---

## Uninstall

```bash
ai-teammates uninstall
```

Unregisters the Claude Code hooks, removes the pre-push hook, deletes
`.ai-teammates/`. Do **not** just `rm -rf .ai-teammates`: the hooks stay
registered, python exits 2 on the missing script, and Claude Code reads that as
*block* — every tool call in the project refused. `cto` stays on your PATH
because other projects use it.
