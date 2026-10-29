# Memory

Memory was the single most persistent failure on the project this system came
from. Not because remembering is hard, but because every design we tried had a
step that depended on someone remembering to do it.

---

## Status 2026-10-02: the choice is open, and seats never touch memory

Everything below the next heading describes the memory history up to 2026-09-25, when
claude-mem was adopted. Two things have changed since.

**Seats do not search memory; the CTO hands them a slice.** A card seat runs with no MCP
servers and none of the user's plugins, so it cannot reach either memory system. The
CTO decides what travels with a task: `memslice.py search "<query>"` previews it,
`memslice.py attach <CARD> "<query>" --pick 1,3` puts the chosen hits on the card
(capped at 800 tokens, stored with their source). It is one interface over both
systems, so the choice of store can change without touching the cards. Scoping is
mandatory (`config.json` `memory`: a MemPalace wing, claude-mem projects); unset is
refused, because an unscoped MemPalace query returned another wing's memory as the top
hit on the pilot project.

**claude-mem was silently not storing anything for two months, and the check said OK.**
Its database lacked three columns its own worker writes (`retry_count`,
`failed_at_epoch`, `completed_at_epoch`): ~10,000 errors since 2026-08-03, repaired
2026-10-02. The framework's check had taken a global maximum over all projects, so
another project's activity kept it green. It is now per project and counts the
worker's store errors (`_config.mem_capture`, with a regression test).

**Head-to-head on the pilot project** (`tools/memory-bench/results/2026-10-02-pilot.md`).
On identical documents MemPalace put the answer at rank 1 for 82% of questions; claude-mem
returned it in its top 5 for none of 11, and for only 6 of 25 of its *own* observations.
claude-mem is cheaper and faster per call; MemPalace captures nothing automatically.
These are different halves of the problem (retrieval vs. capture), the benchmark is 11
questions on one codebase, and the decision between them is the owner's.

**Status 2026-10-03: memory that stays true (`memlife.py`).** Memory only ever grew. A
retired decision that is phrased well outranks the current one, and the slice built for a real
card still told the seat the UI seat ran on "opencode + glm-5.3-flash": one stale line inside an
otherwise correct drawer, next to a drawer that holds both the dead rule and its replacement.
Deleting is wrong (the history is real, and sometimes it is the question) and ranking by age is
wrong (a decision can be old and still right). So four small mechanisms, each visible in the
slice a seat reads:

- **Facts.** One current statement per topic. `fact set` retires the old one, which is kept,
  dated and never returned by a search. Facts matching the query lead every slice.
- **Tombstones** (`memory.retired` in config.json). A line naming a retired term is tagged
  `STALE` in the slice (the line, not the drawer), unless the query itself names the term:
  then you are asking about the history and the old text is the answer. Documents that are
  mostly about retired things sink below the rest. Every hit says how old it is.
- **Supersede / restore** a whole drawer (moved to room `superseded`, labelled, reversible).
- **Decision records.** A merged card is filed as a decision (`memlife.py file`, or
  automatically on `card-clean` (and `land`) with `memory.auto_file`, which is opt-in because it writes to
  the shared palace). `audit` lists documents that still name a retired term and facts nobody
  has re-confirmed in 90 days.

Facts are drawers in room `facts`, not triples in MemPalace's knowledge graph, because the graph
has no embeddings: a fact has to be found by meaning, by the same search that finds everything
else. claude-mem gets the tombstone and age labels on what it returns but no facts or supersede:
it is not ours to rewrite. Executed checks (32, against a throwaway palace) are in
`guardrail_test.py`; the first run found that restoring a drawer by the id a search printed
failed, because that id names the room it came from, not `superseded`.

**Status 2026-10-06: what changed gets recorded (fact review).** In real use (2026-10-02 to
10-05) claude-mem captured 268 observations from the pilot project on its own, and MemPalace gained nothing:
the CTO moved Gemini from Vertex to an API key and no fact said so. A store of current truth stays
current only if someone writes to it, and on this project every design that depended on someone
remembering has failed. So a hook asks, at the moment it matters:

- `session_start.py` records where the integration branch stood when a CTO session began.
- A `Stop` hook (`memlife.py stop-hook`): when a CTO turn ends and commits reached the integration
  branch in this session that no fact review covers, the CTO is stopped once, shown the commits,
  and asked to `fact set` what is now true or `fact reviewed --why "..."` if nothing standing
  changed. Board-only commits, merges and feature branches that have not landed are not counted.
  It asks once per state of the branch and never interrupts a continuation (`stop_hook_active`),
  so it cannot loop. A headless CTO is told to repeat its report afterwards, because its last
  message is the only one the owner reads (measured: a blocking Stop hook's follow-up reply
  replaces the final result of a `-p` run).
- The pre-flight (`memlife.py pending`) lists anything a session left unreviewed, so it reaches the
  next CTO in its brief. Seats are never asked (they record no facts). `memory.fact_review: false`
  turns it off; a project with no MemPalace wing never sees it.

Not built, on purpose: age-based re-ranking (a decay would need a corpus with real dates to be
measured, and a wrong decay buries a correct old decision) and automatic detection of
contradictions (needs a model call per write; the tombstone list is the cheap 80%).

---

## What we tried, and why each failed

**Attempt 1 — two stores, dual-write.** A dedicated memory service alongside a
structured "palace". Reconciling two stores was worse than one; one of them
answered *"no explicit information"* to three phrasings of a question whose
answer it held verbatim. Killed.

**Attempt 2 — hand-rolled capture hooks.** Scripts that parsed session
transcripts and wrote structured facts. They worked when they ran. They ran in
Claude Code, where hooks fire — and **not in the desktop app**, where the
orchestrator actually lived. So the agent doing most of the work wrote nothing
to memory for weeks, and nobody noticed because the capture script itself
reported success.

That is failure shape #1 from the catalogue, applied to memory: a check pointed
at a source that could not see the thing.

**Attempt 3 — a staleness detector.** A hook that warned when memory had not
been written for N days. It correctly reported `ok` while memory was silently
not being captured at all, because it measured *file age*, not *whether capture
worked*.

**What actually fixed it:** stop hand-rolling. Use a memory system whose capture
is not a step anyone can forget, and move the orchestrator to a runtime where
its hooks fire.

---

## claude-mem

[claude-mem](https://github.com/thedotmack/claude-mem) (Apache-2.0). Captures
tool usage during sessions, compresses it, injects relevant context into future
sessions.

```bash
npx claude-mem install                  # Claude Code — the CTO
```

- **5 lifecycle hooks** — SessionStart, UserPromptSubmit, PostToolUse, Stop,
  SessionEnd
- **SQLite + Chroma** — full-text and vector search, local
- **A worker service** with a web viewer
- **3 MCP search tools** in a layered pattern: `search` for a compact index,
  `timeline` for chronological context, `get_observations` for full detail on
  filtered ids only — roughly 10x token savings over fetching everything

Settings: `~/.claude-mem/settings.json`.

---

## Why this one

**Capture is not a step anyone can forget.** It is a lifecycle hook, not a
discipline. Every previous design failed at exactly the point where a human or
an agent had to remember to write something down.

**Seats get memory only through the CTO.** A card seat runs with no plugins or MCP
servers, so it never searches memory itself; the CTO attaches a slice to the card
(`memslice.py`, above). SUPERSEDED 2026-10-02: seats used to share claude-mem through
OpenCode.

**Search is layered.** Naive memory retrieval floods context and gets ignored.
Index-then-fetch keeps it affordable enough to actually use.

**It survives session death.** Our sessions have been OOM-killed three times. A
memory that only writes at clean exit would have lost all three.

**One store.** After killing a dual-write setup, the rule is one source of truth.

---

## The one thing it cannot do

claude-mem records **what happened**. It does not record **what you decided**.

Those are different. A session transcript contains the exploration, the false
starts, and the conclusion, all with equal weight. Six weeks later you need the
conclusion — "we route audits to kimi-k3 because it found three real bugs and
glm found none" — not the forty tool calls that produced it.

So: `board.json` stays, and it is not a duplicate memory store. It is the
**decision log**, committed to the repo, human-readable, deliberately small.
claude-mem holds history; the board holds judgement.

The CTO writes to the board when a decision is made or a measurement is taken.
That is a discipline, and disciplines fail — so the dashboard shows the board's
`updated` date, which makes staleness visible rather than silent.

---

## Verifying memory actually works

Do not trust an installer's success message. Ask a question only a past session
could answer:

```
"What did we decide about routing audits, and why?"
```

If the answer is generic, capture is not reaching the store. Check the worker is
running and that hooks fire in the harness you are using — that is precisely how
the previous system was broken for weeks while reporting healthy.
