# Start here

## Install

```bash
/path/to/AI-teammates/install.sh ~/your-project   # install; from a terminal it goes on into the setup
cto setup             # (what it runs) the questions, the commit, the development checkout; ends with cto --doctor
cd ../your-project-dev && cto                     # start talking
```

`install.sh` writes `.ai-teammates/` into your project (hooks, agent specs,
docs, config, board) and puts `cto` and `ai-teammates` on your PATH. Outside
`.ai-teammates/` it also: registers its hooks in `.codex/hooks.json`
(merging, never replacing yours) and installs a git `pre-push` hook (never over one
it did not write). Runtime state
lives in `.git/ai-teammates/`, shared by every worktree and never committed.

Review the project hooks in Codex with `/hooks`. Trusted hooks run in each Codex CLI session opened in the project, not only the CTO's: the
guard and the push rules apply to all of them, while the session-end commit of the board and
cards happens only in a session started by `cto` (`session.auto_commit` in `config.json`: `cto`,
`all` or `off`). The README has the table.

Re-running updates framework hooks, docs and CTO instructions. On a Claude installation it also
migrates runtime fields on existing seat specs, model defaults, budget settings and hook
registrations. Review the changes before committing them.

`cto setup` asks the questions the installer does not: how Codex CLI is paid for (it also
checks that it is installed and signed in), which memory (claude-mem, MemPalace or both, scoped
to this project), the CTO's model and effort, which seats to keep and on which model, the model
per card tier, the branches, what counts as product code, the delegation policy, the integration
checks, and the daily and per-card time and token limits. Then it sets up the workspace: it offers to remove branches
that are fully merged and worktrees that are stale or idle (only on a yes, each branch tagged
`archive/...` first; nothing unmerged, dirty or remote is touched), commits the install, and
creates the integration branch with its checkout at `../<project>-dev`.
Each default is what is configured now, so re-running it changes only what you change.
`--yes` takes every default; `--answers file.json` scripts it (the keys are in `setup.py`). It is
yours, not an agent's: it refuses inside an agent session, and the guard asks you if one tries.

---

## First session

`cto` runs pre-flight before you can type. The first start in a project (and any start after
the hooks, the config or the tools changed) runs the whole suite, about two minutes:

```
── pre-flight ──────────────────────────
  no seat running
  STATIC — Codex seats and hook registration
  ok    ui-developer: Codex role
  SELF-TEST — it has not passed here yet: running it now ...
  PREPUSH — does it still block product code on main?
  ok    prepush.py BLOCKED a product commit on main (executed, not grepped)
  all guardrails in force
── git ─────────────────────────────────
  on development · 0 dirty · 2 worktrees
  usage today: 0 of 2000000 tokens, 0.0 of 240 minutes (includes reservations)
```

Every later start runs only the checks about this project (under a second) and says so:
`note  self-test not re-run: its 330 checks passed on <date> for exactly these hooks, ...`.
`cto --selftest` runs the whole suite whenever you want it.

Read the last line of each block. "executed, not grepped" is the point: the
check ran the guardrail against a real commit rather than reading its source and
assuming.

Then describe work in plain language. The CTO plans it, hands the product code to
the seats as task cards, reviews what comes back, and tells you what it verified. On a codebase it has not seen, start with the survey in
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
  "cto": { "model": "gpt-6.2-astra", "effort": "xhigh" }
}
```

**`guarded_paths` is the one to get right.** It is what `prepush` and `guard`
consider product code. Too narrow and unreviewed code reaches your main branch;
too broad and every doc edit needs a feature branch, which trains everyone to
override the guardrail — and an ignored guardrail is worse than none.

`install.sh` fills `protected_branches` from the branch your repository ships from and
`guarded_paths` from the source folders it finds; check both. The default CTO uses GPT-6.2-Astra with extra-high reasoning. Verify the installed
CLI’s model and effort with `python3 .ai-teammates/hooks/seat.py route cto --verify`.
[Routing](04-ROUTING.md)

**`integration_checks`** (optional): project commands, such as `["npm run typecheck", "npm test"]`,
that `seat.py land` runs on the merged result every time a card lands. Two cards can each pass
their own checks and break each other; this is where the second one is refused.

**`review`** (optional): `{"sensitive_paths": ["(^|/)billing/"], "sensitive_code": ["charge_customer\\("]}`
adds your project's own regular expressions to what the independent-review gate treats as deciding
access or spend (file paths, and lines a commit adds). The built-in rules cover auth, token,
secret, middleware and policy files in any language. A pattern that is not a regular expression
blocks the promotion and says so.

**`memory.mempalace_bin`** (optional): the path of the `mempalace` command when it is not on `PATH`.

Project daily limits default to `{"daily_tokens": 2000000, "daily_minutes": 240}` in
`budget`. Cards default to 100,000 tokens and 15 minutes. See the [per-run limits and accounting rules](04-ROUTING.md#time-and-token-budgets).

**`delegation`**: `{"cto_product_edits": "block" | "ask" | "allow"}` decides what happens when the
CTO itself edits product code, in any checkout: `block` (the default, also when the key is absent:
product code arrives only through cards), `ask` you each time, or `allow`. `seat.py report --delegation` shows how much
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

Unregisters the Codex CLI hooks, removes the pre-push hook, deletes
`.ai-teammates/`. Do **not** just `rm -rf .ai-teammates`: the hooks stay
registered, python exits 2 on the missing script, and Codex CLI reads that as
*block* — every tool call in the project refused. `cto` stays on your PATH
because other projects use it.
