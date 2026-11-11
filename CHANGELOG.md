# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). Until 1.0, a minor version may change behaviour.

## [Unreleased]

### Added

- **One command from a clone to a working project (#18).** `install.sh <project>` installs into a
  project from anywhere (`ai-teammates install <project>` too), and a first install from a terminal
  goes straight on into `cto setup`. `install.sh <project> --yes` or `--answers file.json` runs the
  setup without asking; `--no-setup` only installs.
- `cto setup` asks more, and finishes the job (#18). **Tool and plan:** it checks that Claude Code
  is installed and signed in and asks whether it is a subscription or API billing (`cto.billing`;
  `seat.py report --cost` says whether its dollars are an estimate or money). **Team:** the CTO's
  effort. **Spend:** `budget.daily_usd` and `cards.default_usd`. **Workspace:** it lists branches
  with nothing the production branch lacks and worktrees that are stale or idle, and removes them
  only on a yes of their own (`--yes` is not that yes); each deleted branch is tagged
  `archive/<branch>-<time>` first; an unmerged branch, a worktree with uncommitted files, the
  protected branches and the remote are never touched. Then it commits the install and creates
  the integration branch with its checkout at `../<project>-dev`. An answer about the plan or the
  spend that cannot be right is refused before anything is written.
- **Cost on the card, and caps (#16).** A card has `budget.usd`, set by the CTO: Claude Code stops
  the run there (`--max-budget-usd`) and the run ends `over_budget`, with nothing committed.
  `card.py check` refuses a cap above the owner's maximum for one run (`cards.max_usd`, 5; default
  cap `cards.default_usd`, 1). `budget.daily_usd` in `config.json` (20; `0` for none): a card that
  could take the day's card runs over it is not launched and the ask is recorded for the owner.
  Every run is appended to its card file (`runs`: status, seat, model, seconds, tool calls, cost,
  tokens) with a running `spent_usd`. `seat.py report --cost` shows spend per day, card and model;
  `seat.py spend` puts today's spend into the CTO's brief. A free-form seat run is capped at the
  maximum for one run, and `cto.max_usd` caps a headless `cto "task"`.
- `AGENTS.md` (and `CLAUDE.md`, which imports it): how an AI agent picks an issue, works in its own
  worktree, tests, and opens a pull request without touching the owner's tools or projects.

### Added

- `guard.py` blocks a project's agents from changing AI-teammates itself (`framework-source`): no
  edit or write into a source checkout, no git write in it, no `gh` pull request or issue from it.
  Reading it and running its scripts stay allowed, and a session in the framework repository is
  not affected. `cto.md` says a framework bug is reported to the owner.
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

- The OpenCode, Codex and Ollama code paths, dead since the seats moved to Claude Code on
  2026-09-25 (#14). `install.sh` no longer writes an `opencode.json` deny-list (one that an older
  install left in a project is not touched: delete it if nothing of yours reads it); `cto setup`
  no longer offers another harness and refuses one in an answers file; `bin/cto` no longer has an
  `ollama launch` path, and a config with `cto.provider` other than `anthropic` stops with the
  reason; `seat.py run` refuses a seat whose spec names another harness.
- The legacy `CONTENTOS_OVERRIDE` variable; `AI_TEAMMATES_OVERRIDE` is the one override.

### Changed

- `cto`, started on the production branch with no integration checkout, points at `cto setup`,
  which creates it (#18). `scripts/test.sh` reaches its development checkout the way a new user
  does: `install.sh <project>`, then `install.sh <project> --yes`.
- **`cto` starts in seconds, not minutes (#17).** The pre-flight ran the whole guardrail suite at
  every start: about 120 seconds, measured, of which under one second checks the project. It now
  runs the project checks every time (`guardrail_test.py --preflight`) and the framework's
  self-test only when the installed hooks, the launcher, `config.json`, a seat spec, the hook
  registration or the versions of python, git or claude changed since it last **passed**, or after
  7 days. A failed self-test is never recorded as passed. The pre-flight says which happened on a
  line the CTO's brief keeps. `cto --selftest` runs all of it; `guardrail_test.py` with no flag
  still does (CI, `scripts/test.sh`); `--project` runs only the project checks.
- **A smaller start for the CTO (#17).** `cto.md` is 1,200 words (it was 2,400) and carries the
  nine failure shapes in one line each; the brief no longer has the whole failure catalogue
  (1,050 words) read at start and no longer repeats the habits. The reasons and the pilot's
  history moved to `docs/05-CTO.md`. The suite holds word limits for both.
- **Delegation is the default (#15).** `delegation.cto_product_edits` is `block` when it is not
  set: the CTO's own edits to product code are refused, with the edit tools and through the shell,
  and it is told to write a card. It was `allow`, and no installer set it. **An upgraded project
  with no `delegation` key changes behaviour**: set `"delegation": {"cto_product_edits": "ask"}`
  or `"allow"` in `config.json` (or `cto setup`) to keep the old one. A new install's config says
  `block`, `cto setup` defaults to it and always writes the choice, a value that is none of the
  three is treated as `block`, and the pre-flight reports an `allow` on a line the CTO's brief
  keeps. `cto.md` says it plainly: the CTO plans, writes cards, reviews and lands.
- The pre-flight's INHERITANCE check asks what Claude Code inherits: that the hook registration
  (`.claude/settings.json`) and the hooks are committed on the production branch. It used to ask
  whether `opencode.json` was. STATIC fails a seat on any harness but `claude-code`. MODELS checks
  the Claude Code sign-in only (#14).
- `seat_status.running_seats()` returns the list of running seats, no longer a pair.
- The dashboard has a new dark design and an **Overview** tab: a bento grid of open work, who holds
  it, guard blocks, branch, memory, cards waiting for review, activity, team and branches, all from
  the live data. The look is inspired by the "Finance Bento" design by vasnev.art; the code is our
  own, with no build step and no external requests (now checked by the test suite). `?still` turns
  the animations off, for screenshots. The roster carries `kind`, `model`, `where` and `state` as
  fields beside its one-line `role`.

### Security

- The independent-review gate ran only on a typed `git merge development` into production.
  `git push origin development:main` promoted the same commits without it. `prepush.py` now runs
  it on every push that lands on production, and blocks when it cannot run.
- `review.py` judged HEAD's diff of a commit's files instead of the commit itself: a flagged
  change was cleared once a later commit touched the same file, and a doc quoting a token
  pattern flagged an unrelated commit. It now reads each commit's own diff, and a git failure
  is an error, not "nothing found".
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

- `seat.py land` refused a card right after running it on a real install: the runner works in the
  integration checkout and leaves its own record there uncommitted (the board note, now the card
  file too), and `land` refused any uncommitted change. The session record no longer counts; any
  other uncommitted change still refuses. The suite missed it because its hooks ran from the
  production checkout (#16).
- `scripts/test.sh` runs the suite's launcher checks against this checkout's `bin/cto`. They ran
  whatever `cto` was already on `PATH` (a developer's installed copy), and were skipped in CI,
  which has none: the sandbox's own `~/.local/bin` was not on `PATH` (#14).
- After an upgrade, a bookmark to the old `/dashboard.html` answered 404. The dashboard server now
  sends it to the page at `/`, unless the folder it serves still has a `dashboard.html` of its own.
- `memlife.py fact set` with an unchanged statement renews its date, so `audit`'s 90-day
  re-confirm can be cleared.
- A memory slice could exceed what `card.py check` accepts (3216 of 3200 characters).
- `card-clean --abandon` kept the seat's uncommitted edits off the archive tag it promised.
- Re-running `install.sh` rewrote `.claude/settings.json` without its final newline, a diff on
  every upgrade. It keeps each file's line ending now; `scripts/test.sh` checks a re-install changes nothing.
- The test suite reports a crashing check as one failure and runs the rest, and
  `scripts/test.sh` reuses MemPalace's embedding model instead of downloading it every run.
- `cto setup --answers` skipped a seat it did not find, without a word: `"backend": {"keep": false}`
  in a project whose backend seat is named otherwise dropped nothing. It now refuses, changes
  nothing, and lists the seats there are by name (a seat is chosen by the `name:` in its spec).
- The setup check copied the installation's own seats, so it failed a correct `setup.py` in any
  project that had renamed or dropped its backend seat. It brings its own seats now.

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
