# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). Until 1.0, a minor version may change behaviour.

## [Unreleased]

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
