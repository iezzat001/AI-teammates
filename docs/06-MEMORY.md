# Memory

Memory was the single most persistent failure on the project this system came
from. Not because remembering is hard, but because every design we tried had a
step that depended on someone remembering to do it.

This page says how memory works now. How it got here is in [History](#history) at the end.

---

## Two systems, two halves

- **claude-mem captures.** It records every Claude Code session through lifecycle hooks, so
  capture is not a step anyone can forget.
- **MemPalace finds.** It holds what is filed (facts, decision records, documents) and is what
  a search reads by default. On the pilot project it put the answer at rank 1 for 82% of
  questions where claude-mem returned it in its top 5 for none of 11
  (`tools/memory-bench/results/2026-10-02-pilot.md`: 11 questions, one codebase).

Nothing writes the same thing to both. `cto setup` configures both by default and lets you keep
one or none.

```bash
npx claude-mem install                 # captures every Claude Code session, by lifecycle hooks
uv tool install mempalace              # or: pipx install mempalace
mempalace init ~/code/your-project     # the palace; the project's wing is config memory.mempalace_wing
cto setup                              # writes the memory block, scoped to this project
```

`cto --doctor` says whether claude-mem is *capturing* for this project, not only installed.
`mempalace` is found on `PATH`, or at `memory.mempalace_bin` in `config.json`.

**Scoping is mandatory.** `config.json` `memory` names a MemPalace wing and the claude-mem
projects. Unset is refused: an unscoped MemPalace query once returned another wing's memory as
the top hit.

## Seats never search memory

A card seat runs with no MCP servers and none of the user's plugins, so it cannot reach either
system. The CTO decides what travels with a task:

```bash
python3 .ai-teammates/hooks/memslice.py search "<query>"                  # preview
python3 .ai-teammates/hooks/memslice.py search "<query>" --from claude-mem  # the other store
python3 .ai-teammates/hooks/memslice.py attach <CARD> "<query>" --pick 1,3  # onto the card
```

A slice is capped at 800 tokens and stored on the card with its source. It is one interface
over both systems, so the store can change without touching the cards.

## Memory that stays true (`memlife.py`)

Memory only ever grows, and a retired decision that is phrased well outranks the current one.
Deleting is wrong (the history is real, and sometimes it is the question) and ranking by age is
wrong (a decision can be old and still right). So four small mechanisms, each visible in the
slice a seat reads:

- **Facts.** One current statement per topic. `fact set` retires the old one, which is kept,
  dated and never returned by a search. Facts matching the query lead every slice.
- **Tombstones** (`memory.retired` in `config.json`). A line naming a retired term is tagged
  `STALE` in the slice (the line, not the drawer), unless the query itself names the term: then
  you are asking about the history and the old text is the answer. Documents that are mostly
  about retired things sink below the rest. Every hit says how old it is.
- **Supersede / restore** a whole drawer (moved to room `superseded`, labelled, reversible).
- **Decision records.** A merged card is filed as a decision (`memlife.py file`, or
  automatically on `card-clean` and `land` with `memory.auto_file`, which is opt-in because it
  writes to the shared palace). `audit` lists documents that still name a retired term and
  facts nobody has re-confirmed in 90 days.

Facts are drawers in room `facts`, not triples in MemPalace's knowledge graph: the graph has no
embeddings, and a fact has to be found by meaning, by the same search that finds everything
else. claude-mem gets the tombstone and age labels on what it returns but no facts or supersede:
it is not ours to rewrite.

## What changed gets recorded (fact review)

A store of current truth stays current only if someone writes to it. So a hook asks, at the
moment it matters:

- `session_start.py` records where the integration branch stood when a CTO session began.
- A `Stop` hook (`memlife.py stop-hook`): when a CTO turn ends and commits reached the
  integration branch in this session that no fact review covers, the CTO is stopped once, shown
  the commits, and asked to `fact set` what is now true or `fact reviewed --why "..."` if nothing
  standing changed. Board-only commits, merges and feature branches that have not landed are
  not counted. It asks once per state of the branch and never interrupts a continuation, so it
  cannot loop. A headless CTO is told to repeat its report afterwards, because its last message
  is the only one the owner reads.
- The pre-flight (`memlife.py pending`) lists anything a session left unreviewed, so it reaches
  the next CTO in its brief.

Seats are never asked (they record no facts). `memory.fact_review: false` turns it off; a project
with no MemPalace wing never sees it.

Not built, on purpose: age-based re-ranking (a wrong decay buries a correct old decision, and
measuring one needs a corpus with real dates) and automatic detection of contradictions (a model
call per write; the tombstone list is the cheap 80%).

## What memory cannot do: the board

claude-mem records **what happened**. It does not record **what you decided**. A session
transcript contains the exploration, the false starts and the conclusion, all with equal
weight. Six weeks later you need the conclusion, not the forty tool calls that produced it.

So `board.json` stays, and it is not a duplicate memory store. It is the **decision log**:
committed to the repo, human-readable, deliberately small. The CTO writes to it when a decision
is made or a measurement is taken. That is a discipline, and disciplines fail, so the dashboard
shows the board's `updated` date, which makes staleness visible rather than silent.

## Verifying memory actually works

Do not trust an installer's success message. Ask a question only a past session could answer:

```
"What did we decide about routing audits, and why?"
```

If the answer is generic, capture is not reaching the store. Check `cto --doctor`, that the
claude-mem worker is running, and that its hooks fire in the session you are using.

---

## History

Kept because each step is a way memory failed while a check said it worked.

- **Two stores with a dual write.** A dedicated memory service next to a structured palace.
  Reconciling them was worse than one store; one answered "no explicit information" to three
  phrasings of a question whose answer it held verbatim. Dropped.
- **Hand-rolled capture hooks.** Scripts that parsed session transcripts. They ran in Claude
  Code, where hooks fire, and not in the desktop app, where the orchestrator then lived. The
  agent doing most of the work wrote nothing to memory for weeks, and the capture script
  reported success. Failure shape 1 of the [catalogue](07-FAILURE-CATALOGUE.md).
- **A staleness detector.** It warned when memory had not been written for N days, and reported
  `ok` while nothing was captured, because it measured file age, not whether capture worked.
- **2026-09-25: claude-mem adopted** as the one store, for the reason that still holds: capture
  is a lifecycle hook, not a discipline, and it survives a killed session.
- **2026-10-02: it had stored nothing for two months, and the check said OK.** Its database
  lacked three columns its own worker writes (about 10,000 errors since 2026-08-03). The
  framework's check took a maximum over all projects, so another project's activity kept it
  green. The check is now per project and counts the worker's store errors
  (`_config.mem_capture`, with a regression test). The same week: seats stopped reaching memory
  directly and get slices, and the head-to-head above was run.
- **2026-10-03: `memlife.py`**, after a slice built for a real card carried one stale line
  inside an otherwise correct drawer.
- **2026-10-06: fact review**, after four days in which claude-mem captured 268 observations
  on its own and MemPalace gained nothing: a provider was changed and no fact said so.
- **2026-10-29: both systems are kept**, as described at the top. Not a dual write: claude-mem
  captures sessions, MemPalace holds what is filed.
