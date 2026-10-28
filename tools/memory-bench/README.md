# memory-bench

Which memory system answers what a task card needs, and at what cost: MemPalace or
claude-mem? This runs both through `memslice.py`, the adapter the CTO uses, on your own
project.

```bash
export BENCH_PROJECT=~/code/my-project       # has .ai-teammates/ installed
cp tools/memory-bench/questions.example.json tools/memory-bench/questions.json
# edit questions.json: questions whose answers are in your board, RULES.md or git history
python3 tools/memory-bench/bench.py check    # every question must be answerable
python3 tools/memory-bench/bench.py load     # put the same documents into both systems
python3 tools/memory-bench/bench.py run      # writes results/<date>-<project>.md
python3 tools/memory-bench/bench.py clean    # remove the benchmark data again
```

Needs MemPalace and a running claude-mem worker. `load` and `clean` write to both stores
under an isolated `bench-<project>` name, never to your project's own wing.

[results/2026-10-02-contentos.md](results/2026-10-02-contentos.md) is the run that informed
[docs/06-MEMORY.md](../../docs/06-MEMORY.md). Results from one codebase do not transfer to
another: measure your own.
