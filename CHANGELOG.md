# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). Until 1.0, a minor version may change behaviour.

## [Unreleased]

### Added

- `AGENTS.md` (and `CLAUDE.md`, which imports it): how an AI agent picks an issue, works in its own
  worktree, tests, and opens a pull request without touching the owner's tools or projects.

### Added

- The dashboard page now ships with AI-teammates (`.ai-teammates/dashboard/`): Board, Branches,
  Team, Rules & Guardrails and Activity, with `#tab` links. The server used to provide only the
  data, and its default folder exposed `config.json`.
- `scripts/dashboard_demo.py`: the dashboard for an invented project, and the README screenshots.
- The README explains how a task flows, and how a teammate differs from a Claude Code subagent.
- Parent cards and dependencies: a parent card (`children`) holds the plan for a task bigger than
  one card; a child names its `parent` and the siblings it `depends_on`. `card.py check` refuses
  cycles, unknown dependencies and mismatched links; the runner refuses a parent, and a child whose
  dependencies are not merged into the integration branch. `card.py tree <ID>` shows the plan;
  `card.py new --parent` links both sides (#1).
- `seat.py land <ID>`: merges a done card in a scratch worktree, re-runs its acceptance on the
  merged result, and fast-forwards the integration branch only if they pass. A failure leaves the
  branch unchanged; a conflict is never resolved, and writes a `merge-conflict` record for the
  owner. The merge commit is recorded on the card's run row (#3).
- The combined result is checked: `config.json` `integration_checks` run on the merged result
  every time a card lands, and `seat.py verify <parent>` runs a parent card's acceptance on the
  integration branch once every child has landed (automatically after the last). The parent is
  done only when it passes (#4).
- `card.py split <ID> --into ID-1,ID-2 [--files ...] [--independent]` turns an oversized card into
  a parent and children that inherit its context, memory and out-of-scope list, with steps and
  acceptance going to the child whose files they name. Without `--files` it proposes a grouping
  by folder. A split that leaves a parent file uncovered, or lets two unordered siblings edit the
  same file, is refused; `card.py check` applies the same checks to a parent, and prints the
  exact `split` command for an oversized card (#2).
- Delegation policy: `config.json` `delegation.cto_product_edits` (`allow`, the default; `ask`; or
  `block`) applies to the CTO's own product-code edits in any checkout, by edit or by shell write.
  Seats and merge-conflict resolution are exempt. `seat.py report --delegation [--days N]` shows
  product lines that reached the integration branch per day, by seats versus by the CTO (#5).
- `cto setup` (also `ai-teammates setup`): the choices a project makes once after install. Memory
  (claude-mem, MemPalace or both, scoped to the project, with the install commands of what is
  missing), the team (the CTO's model; per seat keep or drop, harness and model; the model per card
  tier, optionally verified with one call each), and scope (branches, product paths, delegation
  policy, integration checks). `--yes` and `--answers` for scripts. It refuses inside an agent
  session, and the guard asks the owner when an agent runs it. A dropped seat stays dropped on
  upgrade. `06-MEMORY.md` records the decision to keep both memory systems and how to set them up.

### Removed

- The legacy `CONTENTOS_OVERRIDE` variable; `AI_TEAMMATES_OVERRIDE` is the one override.

### Changed

- The dashboard has a new dark design and an **Overview** tab: a bento grid of open work, who holds
  it, guard blocks, branch, memory, cards waiting for review, activity, team and branches, all from
  the live data. The look is inspired by the "Finance Bento" design by vasnev.art; the code is our
  own, with no build step and no external requests (now checked by the test suite). `?still` turns
  the animations off, for screenshots. The roster carries `kind`, `model`, `where` and `state` as
  fields beside its one-line `role`.

### Security

Gaps found by an analysis of the guardrails, each now executed by the test suite and seen to fail
without its fix:

- `config.json` changed without asking the owner, though it holds the card size limits, the
  protected branches, the product paths, the seats' denied commands and the CTO's pre-approved
  commands: one edit let a card of any size through. Editing it now asks, by edit or by shell
  (redirect, `sed -i`, `git checkout`, an interpreter's heredoc); reading it does not.
- An edit was judged by the checkout the session runs in, so the CTO could edit product code, or
  the hooks, in the production checkout by absolute path. It is now judged by the checkout the
  file is in.
- The pre-push hook could be skipped past the guard with `git -c core.hooksPath=...`,
  `GIT_CONFIG_*`, `git config core.hooksPath`, the abbreviation `--no-veri`, or a git alias that
  runs `push --no-verify`. All are refused now.
- On the production checkout, `git merge feat/development-x` passed as a promotion (a substring
  test), and `cherry-pick`, `revert`, `rebase` and `pull <other branch>` were not checked.
- A seat's deny list missed `"git" push`, `\git push`, `/usr/bin/ssh`, `sudo -u root ...`,
  `nice`/`timeout` wrappers, `$(...)`, and `rm -fr` / `rm -r -f` / `rm --recursive --force`.
- In a card run, `cat a>b`, a redirected self-check, `curl -o`, `tar`, `unzip`, `sort -o`, editors,
  `git add` and git's plumbing writers changed files unnoticed; `git -C .. rm` deleted another
  checkout's file as the card's.
- `card.py check` refuses a card whose acceptance command runs a file the seat may edit.
- `session_end.py` committed everything staged, product code included, as the session record.
- The secret scrubber missed Anthropic, OpenAI project, AWS, fine-grained GitHub and new
  Supabase keys.

### Fixed

- `memlife.py fact set` with an unchanged statement renews its date, so `audit`'s 90-day
  re-confirm can be cleared.
- A memory slice could exceed what `card.py check` accepts (3216 of 3200 characters).
- `card-clean --abandon` kept the seat's uncommitted edits off the archive tag it promised.
- Re-running `install.sh` rewrote `.claude/settings.json` without its final newline, a diff on
  every upgrade. It keeps each file's line ending now; `scripts/test.sh` checks a re-install changes nothing.
- The test suite reports a crashing check as one failure and runs the rest, and
  `scripts/test.sh` reuses MemPalace's embedding model instead of downloading it every run.

## [0.1.0] - 2026-10-28

First public release.

### Added

- `cto`: a Claude Code session with pre-flight checks whose results go into the CTO's brief.
- Guardrails: the `guard.py` PreToolUse hook, the git pre-push hook (`prepush.py`), the
  branch-flow check (`flow.py`), and an independent-review gate for security-touching
  promotions (`review.py`).
- Task cards (`card.py`, `cardrun.py`): one goal, the files a teammate may change, and
  acceptance commands the runner executes. Each card runs in its own worktree with a time and
  tool-call budget.
- `loopguard.py`: an agent blocked on something only a human can fix stops and asks.
- Memory slices for cards (`memslice.py`) and memory that stays current (`memlife.py`:
  facts, retired terms, supersede/restore, fact review on landed work).
- `guardrail_test.py`: the executed test suite, run at every `cto` start.
- `scripts/test.sh` and CI: run the suite in a throwaway project.
- `tools/memory-bench`: compare MemPalace and claude-mem on your own project.
- Contributor docs: CONTRIBUTING, SECURITY, CODE_OF_CONDUCT, issue and pull request templates.

### Changed

- The CTO runs on Anthropic through Claude Code by default. Fresh installs no longer
  default to an Ollama model.
- `install.sh` detects the production branch your repository ships from, and the source
  folders to guard, instead of assuming `main` and `src/ lib/ app/`.
- `install.sh` and `cto` print the exact commands to create the integration branch when a
  project doesn't have one yet.
- The dashboard's owner label comes from `config.json` `owner` instead of a hardcoded name.
- `tools/memory-bench` takes any project (`BENCH_PROJECT`) and your own questions file.

### Fixed

- The hooks' bytecode (`.ai-teammates/hooks/__pycache__/`) is ignored through
  `.ai-teammates/.gitignore`. Before, every hook run left `.pyc` files in projects without a
  Python `.gitignore`, and `git add -A` committed them. The suite now checks for it.
