# Contributing to AI-teammates

Thanks for helping. This guide gets you from a fresh clone to a merged pull request.

## The one rule

**A guardrail must execute, not describe.** A check that reads source code, a log or a
config file and concludes "this is protected" has been this project's most expensive
failure, nine times over ([docs/07-FAILURE-CATALOGUE.md](docs/07-FAILURE-CATALOGUE.md)).

So every change to a guard comes with a check in `core/hooks/guardrail_test.py` that:

1. **runs the real thing** (the hook with a real payload, a real commit through the
   pre-push hook, the real `bin/cto` against a stub `claude`), and
2. **has been seen to fail.** Break your fix on purpose, run the suite, confirm the new
   check goes red, then restore it. A check that never failed may be unable to.

## Set up

You need `git` 2.25+, `python3` 3.9+ and `bash`. Nothing to install: the hooks use only the
Python standard library.

```bash
git clone https://github.com/iezzat001/AI-teammates.git
cd AI-teammates
scripts/test.sh
```

`scripts/test.sh` creates a throwaway project in a temp folder, installs AI-teammates into
it with a fake `HOME` (your real `~/.local/bin` and settings are never touched), and runs the
whole guardrail suite there. It takes one to two minutes and ends with
`all guardrails in force`.

- `claude` is replaced by a stub during the run, so you don't need an account. The suite's
  "signed in" check then proves nothing; run `guardrail_test.py` in a real project for that.
- MemPalace-backed checks run only if `mempalace` is installed (`pip install mempalace`);
  otherwise they print `skip`.
- Keep the temp project for debugging: `KEEP=1 scripts/test.sh`.

## Try it on a real project

```bash
cd ~/code/some-test-project
/path/to/your/AI-teammates/install.sh
```

The installer links `cto` in `~/.local/bin` to your clone, so your edits take effect at once.
After you change hooks, re-run `ai-teammates install` in the project: it copies them in
(`cto` warns when the copies differ).

## Where things are

```
bin/cto                   the launcher: pre-flight, the brief, starting Claude Code
install.sh                installs into a project; also `ai-teammates doctor|uninstall`
core/hooks/               every hook and CLI (Python, standard library only)
  guard.py                PreToolUse: blocks unsafe tool calls
  prepush.py              the git pre-push hook: where a push really lands
  loopguard.py            stops an agent that retries what only the human can fix
  card.py, cardrun.py     task cards and the supervised card runner
  seat.py                 launches teammates
  memslice.py, memlife.py memory slices for cards, and keeping memory true
  guardrail_test.py       the test suite (runs at every `cto` start, and in CI)
core/agents/              teammate specs (copied into a project once; cto.md is updated on upgrade)
core/dashboard/           the dashboard page, served by hooks/dashboard_server.py
docs/                     user documentation (copied into each project)
tools/memory-bench/       MemPalace vs claude-mem retrieval benchmark
scripts/test.sh           run the suite in a sandbox
scripts/dashboard_demo.py serve the dashboard for an invented project; --shots regenerates docs/images
```

## Making a change

1. Open an issue first for anything bigger than a small fix, so we agree on the problem.
   For a new rule, say which incident it prevents (see "Adding a rule" below).
2. Branch from `main`.
3. Make the change, with a test as described above.
4. Run `scripts/test.sh`. It must end with `all guardrails in force`.
5. Add a line to [CHANGELOG.md](CHANGELOG.md) under **Unreleased**.
6. Open a pull request and fill in the template.

## Code style

- **Match the code around you.** Python 3.9, standard library only, no new dependencies.
- **A hook must never break the session.** A bad payload exits 0. A guard that cannot
  decide fails *closed* (refuses), never open.
- **Comments say why, with the evidence.** This codebase records the incident behind a
  rule next to it (`FOUND <date>: ...`). Keep that habit when you fix something real.
- **Measure before you claim.** If a change is about cost or speed, put the numbers and how
  you measured them in the pull request.

## The dashboard

`python3 scripts/dashboard_demo.py` builds an invented project with this checkout's installer and
serves its dashboard on http://127.0.0.1:8790, so you can work on the page without a real project.
If you change what the page shows, regenerate the README screenshots (needs Google Chrome or
Chromium): `python3 scripts/dashboard_demo.py --shots docs/images`.

## Docs

- `docs/` is copied into every project, so write for a user, not for this repo.
- When a decision changes, every doc that says otherwise gets a dated
  `SUPERSEDED <date>` note **in the same pull request**. Agents act on stale docs with
  full confidence.

## Adding a rule

Only after an incident. Describe it in the issue or pull request as:

```
RULE: <one line, imperative>
INCIDENT: <what happened, when, and what it cost>
ENFORCED BY: <the check that executes it>
```

If the last line would be "nothing yet", it is a note, not a guardrail. Say so.

## Reporting bugs and security issues

- Bugs and ideas: [open an issue](https://github.com/iezzat001/AI-teammates/issues/new/choose).
  Include the output of `cto --doctor` and `python3 .ai-teammates/hooks/guardrail_test.py`.
- A way around a guardrail: follow [SECURITY.md](SECURITY.md) instead of a public issue.

By contributing you agree that your contributions are licensed under the
[Apache-2.0 license](LICENSE), and to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
