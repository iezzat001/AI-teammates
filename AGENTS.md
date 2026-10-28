# Instructions for AI agents working on this repository

You are working on **AI-teammates itself**: the framework (hooks, launcher, installer, docs), not a
project it is installed in. Humans: [CONTRIBUTING.md](CONTRIBUTING.md) says the same things at more
length.

## The one rule

**A guardrail must execute, not describe.** Every change to a guard, the card runner, memory or the
installer comes with a check in `core/hooks/guardrail_test.py` that runs the real code, and that
you have **seen fail without your change**. Write the check first, run the suite, watch it go red,
then fix the code. A check that has never failed may be unable to.

## Before you change anything

1. Pick work from the [issues](https://github.com/iezzat001/AI-teammates/issues). The tracking
   issues (e.g. #6) give the order. Take the first open issue whose dependencies are closed, and
   comment that you are working on it.
2. Work in your own worktree, **beside** this checkout, never inside it:
   ```bash
   git fetch origin
   git worktree add ../AI-teammates-<issue> -b feat/<issue>-<short-name> origin/main
   ```
3. Read the files the issue links to. Line links are permalinks to an older commit, so find the
   same code in `main`.

## Test

```bash
scripts/test.sh                                   # the whole suite in a throwaway project, 1-2 minutes
env -u PYTHONDONTWRITEBYTECODE scripts/test.sh    # as CI runs it, if your shell sets that variable
```

It must end with `all guardrails in force`. It runs with a fake `HOME` and a stand-in `claude`, so
it needs no account and touches nothing of the owner's. For the dashboard:
`python3 scripts/dashboard_demo.py` serves an invented project on port 8790.

## Rules of this repository

- **Never run `install.sh` with the real `HOME`** from your checkout or worktree. It links
  `~/.local/bin/cto` and `~/.local/bin/ai-teammates` to the checkout it runs from, which would
  point the owner's tools at your branch. `scripts/test.sh` is the safe way to exercise it.
- **Never touch a project AI-teammates is installed in**, and never run `cto` there, unless the
  owner asks you to.
- **No real project data in this repository:** no hosts, IPs, keys, tokens, customer or project
  names, private board content or paths under the owner's home. Examples and screenshots use
  invented data (`scripts/dashboard_demo.py`).
- **Python 3.9, standard library only.** No new dependencies. Match the surrounding code; comments
  say why, with the evidence (`FOUND <date>: ...`).
- **A hook must never break the session** (a bad payload exits 0), and **a guard that cannot
  decide refuses**.
- **Never bypass a git hook** (`--no-verify`, `core.hooksPath`). If a guard fires on correct work,
  say so in the pull request and propose the fix.
- When behaviour changes, update the docs that describe it in the same pull request. A doc that is
  now wrong gets a dated `SUPERSEDED <date>` note. Add a line under **Unreleased** in
  [CHANGELOG.md](CHANGELOG.md).

## Finish

1. `scripts/test.sh` passes; your new checks failed before the fix.
2. Push your branch and open a pull request against `main` with `gh pr create`. Fill in the
   template, put `Fixes #<issue>` in the description, and say how you saw each new check fail.
3. **Never push to `main` and never merge your own pull request.** The owner reviews and merges.
4. When it is merged, remove your worktree:
   `git worktree remove ../AI-teammates-<issue> && git branch -d feat/<issue>-<short-name>`.

## Where things are

| Path | What it is |
|---|---|
| `bin/cto` | The launcher: pre-flight, the brief, starting Claude Code |
| `install.sh` | Installs into a project; also `ai-teammates doctor` and `uninstall` |
| `core/hooks/guard.py` | PreToolUse hook: blocks unsafe tool calls |
| `core/hooks/prepush.py` | The git pre-push hook |
| `core/hooks/card.py`, `cardrun.py`, `seat.py` | Task cards, the supervised card runner, seat launches |
| `core/hooks/loopguard.py` | Stops an agent that retries what only the owner can fix |
| `core/hooks/memslice.py`, `memlife.py` | Memory slices for cards; keeping memory true |
| `core/hooks/guardrail_test.py` | The test suite (runs at every `cto` start, and in CI) |
| `core/agents/` | Seat specs and `cto.md`, the CTO's instructions |
| `core/dashboard/` | The dashboard page |
| `docs/` | User documentation, copied into each project |
| `scripts/` | `test.sh` (the suite in a sandbox), `dashboard_demo.py` (demo and screenshots) |
