# Guardrails

Every rule here exists because something broke. The incident is named so you can
judge whether the rule applies to your project — and delete it if it does not.

**A guardrail must execute, not describe.** Three of ours passed for weeks while
protecting nothing, because the test grepped source code instead of running it.

---

## The hard rules

### 1. Product code never lands directly on a protected branch

`src/**` and equivalent go on a feature branch in its own worktree. Config and
docs may go direct — that is deliberate, so a doc fix is not a ceremony.

Enforced by `prepush.py`, installed as the git **pre-push hook** in the common
git dir. Git tells it where each ref actually lands, so it cannot be dodged by
spelling (`feat:main`, `HEAD`, `git -C`), and it fires in every harness and
terminal, for every worktree. An earlier version guessed the destination from
the command text inside Claude Code only; `git push origin feat:main` walked
through.

**Incident:** product code reached the protected branch four times without
review.

**The counter-incident, which matters more:** the first version compared
`git diff upstream..HEAD` and could not tell a file that arrived by *merging a
reviewed branch* from one committed directly. The integration branch exists to
accumulate merges, so it became permanently unpushable — 32 reported violations,
6 real. People overrode it by habit, and a genuine violation went through
unnoticed. It now uses `--first-parent --no-merges`.

> A guardrail that fires on correct behaviour trains people to ignore it. Fix it
> the day you notice.

### 2. Overrides are allowed and must be announced

The CTO may override with `AI_TEAMMATES_ALLOW='<why>' git push ...` — the
environment of git, so it works from any harness, and it is logged to
`events.jsonl`. The condition is that it says so, naming the files and the
reason. (`--no-verify` is blocked: it skips the check instead of announcing it. So are its other
spellings, found 2026-10-28: git's abbreviation `--no-veri`, `-c core.hooksPath=...` or
`GIT_CONFIG_*` pointing git at another hooks folder, and an alias that runs `push --no-verify`.)

An override announced is a judgement call. An override unmentioned is the
guardrail not existing.

Overriding to save two minutes of moving a commit onto a branch is the wrong
reason. That has already happened twice.

### 3. Never print a secret

Not in a log, an error body, a commit, a chat message, or a command line. Move
keys file-to-file. Use stdin for `secret put` so the value never reaches a shell
history.

**Incident:** a token lived in client code and shipped to every browser. Later, a
"key dialog" was requested in chat and refused — anything pasted into a
transcript is in the transcript forever.

### 4. Every expensive operation checks before it executes

Scrapes, transcriptions, model calls that bill per token.

**Incident:** a profile limit of 50 produced 454 requests, hit a data ceiling,
aborted without writing results, billed $0.58, and reported SUCCEEDED with zero
output. The limit is now 18.

### 5. Read-only by default for anything analytical

Audits run in a mode that **cannot write**. Not "is instructed not to write" —
cannot.

### 6. One agent at a time on a given worktree

**Incident:** the orchestrator read a log that only fills on process exit,
declared a live run dead, and nearly started a second concurrent run of an
expensive job. Liveness is a process-table question: `pgrep`, elapsed, CPU.

### 7. Hooks are per-worktree copies and they drift

**Incident:** four worktrees carried four different versions of the same
guardrail. One carried **none at all** — the active UI worktree, 120 commits
behind, had no hooks whatsoever and nobody knew.

`guardrail_test.py` asserts parity across every worktree. One exception, and only one:
a checkout on the production branch whose hooks are exactly what is committed there,
behind an integration branch that contains it, is reported as a **note** (the next
promotion levels it). **Counter-incident (2026-10-03):** that checkout failed the check
after every framework upgrade until the next promotion, a failure guaranteed by the
branch flow itself; the CTO correctly called it the rule that cannot be satisfied and
gets ignored. Any other drift still fails.

### 8. Ask rather than guess

A two-second question beats a twenty-minute exploration built on a wrong
assumption. This is a rule about *cost*, not politeness.

### 9. A seat runs only what it was given, even through the shell

A card may grant its seat extra **commands** (`Bash(npm run typecheck)`,
`Bash(git diff:*)`), never a tool, a shell, an interpreter, a file writer, git beyond
reading, or npm installing: `card.py check` refuses the card. While a card runs,
`guard.py` refuses any shell command that would change files (a redirect, `cp`,
`sed -i`, `git checkout`, `tee`, or a shell or interpreter it cannot read into); files
change only through Edit/Write on the card's own files, where the scope check sees
them, and `git rm` of a file the card names (deleting is a change too, and no edit tool
can do it). Every seat is refused `config.json` `required_denies` (`git push`, `rm -rf`,
`ssh`, and per project e.g. `supabase*`) however it is run (`git -C x push`, `npx`,
`bash -c '...'`, `xargs`, `find -exec`); an argument that only names one is not one.

**Incident (2026-10-03):** the security-audit seat, auditing this framework before a
promotion, found that a card's grants went into `--allowedTools` unchecked, that the
live scope check watched edit tools only, and that `required_denies` was asserted by
the test suite and enforced by nothing in the hooks. All three now execute in
`guardrail_test.py` (SEAT LIMITS), and each was removed once to prove a check fails.

**Counter-incident (2026-10-04), found by the pilot project's CTO:** the first fix tried every
word as a possible command, so with `supabase*` denied, `grep -rn "supabase" src/` was
refused as if it ran the Supabase CLI, and the read-only audit seat could not read the
product's auth layer (three refusals per run, no verdict). And because a seat could not
delete a file, the CTO did a deletion card itself. Both fixed; the test now uses the pilot project's
real deny list and checks that reading about a denied tool still works.

### 10. A project's agents do not change AI-teammates itself

The CTO and its seats may read the framework's source and run its scripts. They may not edit it,
write into it, run a git write in it (branch, commit, worktree, push) or a `gh` action from it (a
pull request, an issue): `guard.py` blocks it (`framework-source`). A source checkout is known by its
files (`install.sh`, `bin/cto`, `core/hooks/guard.py`), so this covers the dev repository, the stable
copy `cto` runs from and any clone. A session working in the framework repository itself is not
affected. A framework bug is reported to the owner; it is fixed and reviewed in its own repository.

**Incident (2026-10-29):** the pilot project's CTO was asked to fix a failing pre-flight check. The
check was the framework's, so it fixed the framework's test in its project copy, then read the
AI-teammates repository's own `AGENTS.md` and set out to branch it and open a pull request there.

Known limits: a script or interpreter run inside the framework checkout is not read (running the
framework is allowed), and nor is a `gh` action that names the repository with `--repo` from
elsewhere; it would need a branch pushed from the checkout, which is blocked.

---

## What runs, and when

| Check | When | What it proves |
|---|---|---|
| `guardrail_test.py` | Every CTO session start | Seats are bound by permissions; `prepush` blocks a real commit — by **executing** it |
| `prepush.py` | Every push (git pre-push hook) | No unreviewed product code on a protected branch; production only via integration; a push onto production carrying commits that decide access or spend needs a filed independent review (`review.py`), however the push is spelled, and is blocked if the check cannot run |
| `guard.py` | Claude Code `PreToolUse` | Product files are not edited in a checkout on a protected branch, judged by the checkout the file is in; changing a guardrail (the hooks, `.claude/settings*.json`, `config.json`) asks the owner, by edit or by shell; a card seat edits only its card's files and writes nothing through the shell; no seat runs `required_denies`; the CTO's own product-code edits follow `config.json` `delegation` (allow, ask or block); nothing changes the AI-teammates source itself |
| `seat_status.py` | Before launching a seat | Nothing is already running — from the **process table** |
| `check_build_env.py` | Before every deploy | Config is actually baked into the **built bundle** |

Note what each looks at. `guardrail_test` executes `prepush` against a scratch
commit and checks it returns non-zero. `check_build_env` greps the compiled
output, not the `.env` that fed it. Each was written after the descriptive
version of the same check passed while protecting nothing.

---

## Environment traps

These are not policy, they are landmines that cost days.

- **A shadowing binary.** `/usr/local/bin/git` was 2.19 from 2021, shadowing
  2.50. `git add --sparse` failed **247 times** silently inside a harness.
  Hooks now prefix `PATH="/usr/bin:$PATH"`.
- **Build-time vs runtime config.** `VITE_*` vars are baked into the browser
  bundle **at build time**. Server-side secrets do not help. A build from a
  worktree with no `.env` ships a bundle that fails for every visitor, while the
  deploy reports success.
- **Per-worktree gitignored files.** `.env` does not travel with a branch. Every
  new worktree starts without one.
- **Memory.** On a small machine, a harness plus a dev server plus a desktop app
  is an OOM kill. It happened three times.

---

## Adding a rule to your own project

Only after an incident. Write it as:

```
RULE: <one line, imperative>
INCIDENT: <what happened, with the date and the cost>
ENFORCED BY: <the script that executes, or "nothing yet — honour system">
```

If the third line says "nothing yet", the rule will be forgotten. That is not an
argument for skipping it — it is an argument for being honest that you have a
note, not a guardrail.
