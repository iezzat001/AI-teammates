# The team

Teammates are single-purpose agents the CTO launches for bounded work. Each is a
**harness + model + permission set + worktree**. All four matter; changing any
one changes what the teammate is.

---

## Anatomy of a teammate

A seat is a markdown file in `.ai-teammates/agents/` whose frontmatter is read by
`seat.py` (a deliberately tiny parser: one `key: value` per line, no lists):

```
---
name: ui-developer
seat: UI Developer
harness: claude-code
model: claude-sonnet-5-5
writes: true                       # may change files (a card seat). Or: read_only: true
allow: Read,Glob,Grep,Edit,Write   # tools it may use without asking; a card can add more
worktree: ../PROJECT-ui            # only for free-form runs; card runs get their own
branch_prefix: feat/ui-
---
```

- **harness** — the runtime. Cards run on `claude-code`, because that is where the
  hooks that enforce scope, loops and budget fire.
- **model** — the default brain; a card's `tier` picks the model per card (see
  [04-ROUTING](04-ROUTING.md)).
- **writes / read_only** — a seat that changes files must say so; the safety test
  refuses a writer whose launch is not `acceptEdits` + prompts denied + allowlist.
- **worktree** — its own checkout. A card run creates a fresh one per card.

The launch command for a card is built by `cardrun.card_command`, not written in
the spec, so the permission flags cannot drift away from what is tested. The unit of
work is a **task card**, not a free-form instruction: see [05-CTO](05-CTO.md) and
`cto.md` for how one is written and run (`seat.py run --card <ID>`).

---

## The roles

Start with these four. Add only when a real task does not fit.

### UI developer
Components, layout, styling, screenshot-driven iteration. Card seat on Claude Code
(`writes: true`). Models are multimodal, so a card can ask it to compare against a
screenshot. Cannot push, deploy, touch secrets, or edit outside the card's files.

### Backend
API endpoints, data access, migrations. Card seat on Claude Code (`writes: true`).
Use the `hard` tier when the task is a reasoning problem rather than a typing problem.
Cannot push, deploy, or `ssh`. Schema changes are reviewed before they run.

### Security audit
Read-only. Finds problems, fixes nothing.
**Claude Code `--permission-mode plan`** — cannot write, by construction, not by
instruction. Run it on the `hard` tier: audits are where a strong model has repaid
its cost.

### Infrastructure
Deploys, DNS, server config.
**The CTO does this itself.** Teammates are denied `ssh`. Not a capability
limit — an accountability one: the CTO reviews the diff, so the CTO carries the
deploy.

---

## What binds a seat

A card seat launches with `--permission-mode acceptEdits --permission-prompts none`
and an explicit tool allowlist. Anything outside the allowlist would need an
approval, and with no one to ask it is denied; `loopguard` turns that denial into a
`BLOCKED` report and ends the run rather than letting the seat try variants. The
hooks (`guard.py`) deny any edit outside the card's `scope.edit` while it works.
`--setting-sources project,local --strict-mcp-config` keep the seat away from your
own plugins and MCP servers, so memory reaches it only as the slice on the card.

`guardrail_test.py` asserts this against the argv that would actually launch, and
executes the scope guard, rather than trusting the docs.

(OpenCode seats are bound by `opencode.json` instead. Cards no longer run there; see
the History note in [04-ROUTING](04-ROUTING.md).)

---

## Launching one

```bash
python3 .ai-teammates/hooks/card.py new DOC-2 --seat ui-developer --tier mechanical --title "..." --goal "..."
python3 .ai-teammates/hooks/card.py check DOC-2
python3 .ai-teammates/hooks/seat.py run --card DOC-2
```

The runner handles what has bitten us:

- checks the **process table** for an existing run of that seat
- refuses a card that is too big to be one task, **before** spending anything
- refuses a parent card (the plan for a bigger task: `card.py tree <ID>`), and a child
  card whose `depends_on` cards are not merged into the integration branch yet
- gives each card its own worktree and branch off the integration branch, and
  archives (tags) an earlier unfinished attempt instead of reusing its leftovers
- kills a run that passes its wall-time or tool-call budget, and its children
- computes the diff from git, checks it against the card's scope, **executes** the
  acceptance commands, and only then commits as `seat:<name>`
- records status, cost, branch, files and acceptance results on the run, and moves
  the board card

---

## Adding a teammate

Only when a real task does not fit an existing role. Then:

1. **Name the task type**, not the technology. "Data migration", not "Postgres".
2. **Pick the harness first.** Hooks needed → Claude Code. Writes product code →
   `writes: true`. Read-only → `read_only: true` (plan mode).
3. **Pick the model from measurement.** Run the candidate on one real card from
   your repo and compare. Leaderboards do not know your codebase.
4. **Write the allowlist before the first run**, not after.
5. **Write a card for it**, and run that, not a free-form instruction.
6. **Record the evidence** for the choice on the board.

---

## What teammates are bad at

Recorded so the CTO stops re-learning it:

- **Long multi-step work.** Quality degrades past a few steps. Split it.
- **Knowing when they have failed.** A harness that stalls emits a confident
  report and no diff. Codex has done this twice. Always check `git status`.
- **Noticing a stale doc.** They will follow a doc naming a branch deleted two
  weeks ago, with total confidence.
- **Cost awareness.** They will re-run an expensive scrape without noticing.
  Deny it, or check the cache before the call.

And the one thing they are unexpectedly good at: **disagreeing with a wrong
brief**, when you explicitly invite it. That has caught more real problems than
any single check in this repo.
