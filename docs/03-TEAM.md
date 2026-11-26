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
harness: codex
model: gpt-6.2-sol
writes: true                       # may change files (a card seat). Or: read_only: true
worktree: ../PROJECT-ui            # only for free-form runs; card runs get their own
branch_prefix: feat/ui-
---
```

- **harness** — the runtime, always `codex`: that is where the hooks that
  enforce scope, loops and budget fire. Any other value is refused.
- **model** — the default brain; a card's `tier` picks the model per card (see
  [04-ROUTING](04-ROUTING.md)).
- **writes / read_only** selects the workspace-write or read-only sandbox.
- **worktree** identifies the checkout for a free-form audit. Each card gets its own worktree.

`seat.py run --card <ID>` reads the seat and the card, then launches a supervised Codex thread.

## The roles

Start with these four. Add only when a real task does not fit.

### UI developer
Components, layout, styling, screenshot-driven iteration. Card seat on Codex CLI
(`writes: true`). Models are multimodal, so a card can ask it to compare against a
screenshot. Cannot push, deploy, touch secrets, or edit outside the card's files.

### Backend
API endpoints, data access, migrations. Card seat on Codex CLI (`writes: true`).
Use the `hard` tier when the task is a reasoning problem rather than a typing problem.
Cannot push, deploy, or `ssh`. Schema changes are reviewed before they run.

### Security audit
Read-only. Finds problems, fixes nothing.
The read-only sandbox restricts filesystem writes. Use the hard tier for complex reviews.

### Infrastructure
Deploys, DNS, server config.
**The CTO does this itself.** Teammates are denied `ssh`. Not a capability
limit — an accountability one: the CTO reviews the diff, so the CTO carries the
deploy.

---

## What binds a seat

The supervisor selects the sandbox and disables approval requests and native subagents.
Trusted pre-tool hooks check allowed commands and every path in a patch. The runner checks
the finished Git diff and acceptance commands again. Scope and budget come from the card.

```bash
python3 .ai-teammates/hooks/card.py new DOC-2 --seat ui-developer --tier mechanical --title "..." --goal "..."
python3 .ai-teammates/hooks/card.py check DOC-2
python3 .ai-teammates/hooks/seat.py run --card DOC-2
```

The runner handles what has bitten us:

- checks the **process table** for an existing run of that seat
- refuses a card that is too big to be one task, **before** spending anything; `card.py
  check` prints the `card.py split` command that turns it into a parent and child cards,
  and a split that leaves a file uncovered or lets two parallel children edit one file is refused
- refuses a parent card (the plan for a bigger task: `card.py tree <ID>`), and a child
  card whose `depends_on` cards are not merged into the integration branch yet
- gives each card its own worktree and branch off the integration branch, and
  archives (tags) an earlier unfinished attempt instead of reusing its leftovers
- stops a model run at the wall-time deadline or first token report at or above its cap
- reserves the run's time and token allowances against shared daily limits

- computes the diff from git, checks it against the card's scope, **executes** the
  acceptance commands, and only then commits as `seat:<name>`
- records status, seconds, tokens, branch, files and acceptance results on the run, appends
  the run and token total to the card file itself (`runs`, `spent_tokens`), and moves the board card
- `seat.py land <ID>` merges a done card in a scratch worktree, re-runs its acceptance on
  the merged result, and only then moves the integration branch. A failure or a conflict
  leaves the branch as it was; a conflict is recorded for the owner, never resolved.
  `config.json` `integration_checks` run there too, every time a card lands
- `seat.py verify <parent>` runs a parent card's acceptance (the whole outcome) on the
  integration branch once every child has landed; `land` runs it after the last one

---

## Choosing the team

`cto setup` keeps or drops each seat (a dropped one moves to `.ai-teammates/agents/off/` and an
upgrade leaves it there), sets each seat's model in its spec, and the model per card
tier (`seat_models`). Every seat runs on Codex CLI. In an answers file a seat is named by
the `name:` in its spec, not its file name; a name that is not a seat refuses and changes nothing.

## Adding a teammate

Only when a real task does not fit an existing role. Then:

1. **Name the task type**, not the technology. "Data migration", not "Postgres".
2. **Say what it may do.** The harness is always `codex`. Writes product code →
   `writes: true`. Read-only → `read_only: true` (read-only sandbox).
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
