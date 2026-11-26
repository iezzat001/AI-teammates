# Routing: which seat gets the card, on which model

## Codex CLI team

The CTO runs on GPT-6.2-Astra with extra-high reasoning (`xhigh`). Choose the seat
by ownership, then choose the worker model by the difficulty of the card. A backend seat can
use Luna for a rename and Sol for a migration.

| Role or card tier | Model | Reasoning | Example |
|---|---|---|---|
| CTO | `gpt-6.2-astra` | `xhigh` | Decompose a change across services, resolve competing requirements, sign off on the result |
| `mechanical` | `gpt-6-luna` | `low` | Apply a known transformation with an exact, executable acceptance check |
| `standard` | `gpt-6.2-sol` | `medium` | Implement a specified endpoint or component, add tests, review an ordinary change |
| `hard` | `gpt-6.2-sol` | `high` | Reason about retries, concurrency, migrations, or access-control correctness |

Keep Astra off the normal worker routes. Move a card from Luna to Sol when its check exposes
a case the mechanical edit missed. Give the CTO the evidence if a hard card still needs an
architectural decision. A missing credential or ambiguous requirement should go to the owner.

Inspect the route for a tier or a stored card. The latter respects an explicit `card.model`.
`--verify` checks the model and effort together against the selected CLI's catalog.

```sh
python3 .ai-teammates/hooks/seat.py route cto --verify
python3 .ai-teammates/hooks/seat.py route --card DOC-2 --verify
```

The command uses `codex` from PATH. Set `AI_TEAMMATES_CODEX_BIN` to select a different
executable. Two CLI installations on one machine can advertise different models.

**Measured 2026-11-26.** A read-only CLI trial used invented inputs, one invocation per model
and fixture, with four expected outputs per fixture. The rename fixture distinguished fields
from local variables and serialized keys. The retry fixture traced failed writes and duplicate
jobs. Luna low matched 3/4 on each, Luna medium matched 3/4 and 2/4, and Sol medium matched
4/4 on both. On a separate lease and queue fixture, Sol high and Astra xhigh both matched all
four traces against a reference implementation. Use the project's own card outcomes, including
retries, to refine these starting tiers.

## Time and token budgets

| Scope | Tokens | Time |
|---|---:|---:|
| Default card or read-only audit | 100,000 | 15 minutes |
| Maximum card and default headless CTO | 500,000 | 30 minutes |
| All supervised runs per project and local day | 2,000,000 | 240 run-minutes |

Set `budget.tokens` and `budget.minutes` on a card. In project config, use
`cards.default_tokens`, `cards.default_minutes`, `cards.max_tokens`, and `cards.max_minutes`.
A headless CTO accepts `cto.max_tokens` and `cto.max_minutes`. Daily limits are
`budget.daily_tokens` and `budget.daily_minutes`. Setting a daily dimension to zero disables
that dimension. Per-run caps must be positive. Tool-call and idle limits also apply to cards.

Each run reserves both allowances before launching. Sibling worktrees share a ledger in
the common Git directory. Two simultaneous 10-minute runs reserve 20 run-minutes. Usage
belongs to the local date the run started. A completed run releases the unused reservation.
After an interrupted request the ledger holds at least the token reservation because final
usage may be unavailable. If the supervisor itself dies, the reservation remains for that day.

The supervisor measures elapsed time with a monotonic clock and counts cumulative input and
output tokens from Codex. Cached input and reasoning output are already included. It stops
at the first update at or above the token cap. One model response can exceed the threshold
before Codex reports it. The runner records the observed usage and refuses to commit the card.
A completed turn without token usage also fails. The caps cover the model session. Project
setup and acceptance commands have their own execution characteristics.

Use `seat.py spend` to see the day’s usage and reservations and `seat.py report --usage` for
seat history by date, card, and model. Bare `cto` opens the interactive Codex TUI. Its own
conversation is outside the supervised budget. Seats it launches still use their budgets.

## Running a delegated card

In an interactive CTO session, the CTO writes and checks a card, then requests approval
for the runner when it needs to create a sibling worktree. For an unattended handoff, run
`cto --card DOC-2` from the terminal. The launcher reserves the CTO review budget, runs
the named card on its selected worker, and gives Astra the runner’s verified result.
A failed or capped card exits with the runner’s status before a review turn starts.
Headless shell calls use built-in commands and the project’s exact `cto.allow` prefixes.
A free-form headless CTO can prepare cards but cannot grant itself permission to start a
process outside its sandbox.

## Upgrading a Claude-based project

**SUPERSEDED 2026-11-26.** Codex CLI now runs the CTO and the seats. Time and token caps
replace the former USD settings. There is no price conversion. Existing cards without token
caps inherit the project default. Past run records remain available and daily reservations
start with the new ledger.

Re-run the project installer, review the migrated config, and commit the install as usual.
Open the main project in Codex, trust the project, and review its registered commands in
`/hooks` before the first team run. Launches check that the required hooks are enabled and
trusted. Use `cto setup` to choose limits for this project's workload.
