# Workflow: branches, worktrees, dashboard

---

## The layout

Four long-lived worktrees. Not five, not nine. We ran nine for a week and it cost
a day to untangle — two branches turned out to contain **zero files** the main
branch lacked, while appearing to hold work.

```
project/            main                      what production runs
project-dev/        development               integration; deploys to staging
project-ui/         feat/ui-*                 ongoing UI work
project-<feature>/  feature/<name>            one live feature
```

Seat worktrees (`project-backend/`, `project-audit/`, …) are separate and
**ephemeral**: `seat.py` creates one from the integration branch when a seat
first runs. They are scratch space for delegated runs, not places to keep work.

**Worktrees, not branch switching.** Each has its own checkout, so an agent can
work in one while you work in another. Switching branches under a running agent
is how files end up in the wrong commit.

**Scratch worktrees live outside the repo.** An agent once created one at
`.claude/worktrees/` inside the project, then committed a `.gitignore` entry to
hide it. Siblings of the project directory, always.

---

## Flow

```
feature branch → development → main → production
    agent          you test      you approve   you deploy
```

- **Feature branches** are where agents work. One agent, one worktree.
- **development** accumulates merged features. It exists to be broken.
- **main** is what production runs. Product code arrives only by merge.
- **Deploys are separate from merges.** Merging changes a git ref and nothing
  else. This surprises people: production keeps serving the old build until
  someone deploys.

---

## Before deleting a branch

Two branches once looked like unmerged work. Checking properly:

```bash
git diff --diff-filter=A --name-only main..$B | wc -l   # unique files: 0
git diff --stat main..$B | tail -1                       # would DELETE 6537 lines
```

They were entirely **behind** main. Merging them would have deleted six weeks of
work while looking like integration.

```bash
git merge-base --is-ancestor $B main && echo merged
git tag archive/$B $B      # a tag costs nothing and makes it reversible
git branch -D $B
```

Always tag before deleting. Always.

---

## Keeping worktrees level

Drift is the enemy. A worktree 120 commits behind is not a branch, it is a
different project — and ours had **no guardrail hooks at all** as a direct
result, because the hooks were committed after it was created.

Merge main into every long-lived worktree regularly. `guardrail_test.py`
asserts hook parity across all of them.

---

## Environment files do not travel

`.env` is gitignored, therefore **per-worktree**. A new worktree starts without
one. A build from a worktree with no `.env` produces a bundle with no config —
and deploys successfully, and fails for every visitor.

This is worth internalising: **build-time config is not runtime config**.
Server-side secrets do not save a browser bundle that was built without them.
`check_build_env.py` greps the built artifact, which is the only place the
answer actually lives.

---

## The dashboard

```bash
python3 .ai-teammates/hooks/dashboard_server.py      # http://127.0.0.1:8899; add a port number for another
```

A local page with five tabs (Board, Branches, Team, Rules & Guardrails, Activity), served on
127.0.0.1 only. The page ships in `.ai-teammates/dashboard/` and is upgraded with the hooks. To
use your own page instead, point `dashboard_dir` in `config.json` at its folder. `#team`,
`#rules` and so on in the address open that tab.

| Endpoint | Serves |
|---|---|
| `/git.json` | Branches, worktrees, ahead/behind, dirty files: computed per request, never cached |
| `/board.json` | `.ai-teammates/board.json`, with the team roster generated from the agent specs |
| `/team.json`, `/rules.json` | Generated from the agent specs, `config.json` and `RULES.md`: the sources the hooks enforce from |
| `/health.json` | Whether memory capture is working, and how old the board is |
| `/events.jsonl` | Every tool call, block and override the hooks logged |

Try it without a real project: `python3 scripts/dashboard_demo.py` in the AI-teammates checkout
builds an invented one and serves it on port 8790.

The line worth putting in any UI header — `main · 4 branches · 4 worktrees · 2
dirty` — is the fastest way to catch drift. If it says dirty and you did not
leave something dirty, an agent did.

---

## The board

`.ai-teammates/board.json`. Committed, so it travels with the repo and every
agent reads the same state.

```json
{ "id": "AUTH-3", "title": "...", "agent": "cto", "status": "todo",
  "updated": "2026-09-10", "note": "the finding, with numbers" }
```

The `note` is the valuable field. Write what was *measured* — "top score fell
0.54 → 0.055, cos(POS,NEG) is 0.898" — not "investigate ranking". Six weeks
later the number is the only thing that still means anything.

**Check for id collisions before appending.** Two ids were once reused; the
write silently did nothing and the commit claimed otherwise.
