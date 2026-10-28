#!/usr/bin/env python3
"""MemPalace vs claude-mem on YOUR project: which one retrieves what a card needs, cheaply?

    export BENCH_PROJECT=~/code/my-project          # a project with AI-teammates installed
    cp tools/memory-bench/questions.example.json tools/memory-bench/questions.json   # then write your own
    python3 tools/memory-bench/bench.py check       # can the corpus answer each question?
    python3 tools/memory-bench/bench.py load        # Arm B: the SAME documents into both systems
    python3 tools/memory-bench/bench.py run         # score both arms, write results/<date>-<project>.md
    python3 tools/memory-bench/bench.py clean       # remove the benchmark fixtures

Written for ContentOS (results/2026-10-02-contentos.md is that run); the project, its live
memory scoping (.ai-teammates/config.json "memory") and the questions are now inputs.

Both systems are queried through memslice.py, the adapter the CTO actually uses to
build a card's memory slice, so this measures what a card would receive: same wing/
project scoping, same k, same token estimate.

ARM A  your live stores exactly as they are. Measures what you get today, INCLUDING
       whatever each system failed to capture (claude-mem could not store anything
       from 2026-08-03 until it was repaired on 2026-10-02).
ARM B  one identical set of documents (board cards, RULES.md, git history) loaded into
       both under an isolated name. Isolates retrieval quality from capture history.

Scoring: a question is answered at rank r when hit r contains ALL of the question's
`must` regexes. hit@1/3/5, MRR, tokens returned, tokens until the first answering hit,
latency as the adapter experiences it (MemPalace pays a python start per call).
A question the corpus cannot answer is reported INVALID, never silently scored.
"""
import json
import os
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
import urllib.request
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "core" / "hooks"))
import memslice  # noqa: E402

if not os.environ.get("BENCH_PROJECT") and sys.argv[1:2] != ["--help"]:
    sys.exit("set BENCH_PROJECT to the project to benchmark (it needs .ai-teammates/ installed), e.g.\n"
             "  BENCH_PROJECT=~/code/my-project python3 tools/memory-bench/bench.py check")
TARGET = Path(os.path.expanduser(os.environ.get("BENCH_PROJECT", "."))).resolve()
NAME = TARGET.name
QUESTIONS = Path(os.environ.get("BENCH_QUESTIONS") or HERE / "questions.json")
WORK = Path("/tmp/memory-bench")
WING = PROJECT = f"bench-{re.sub(r'[^a-z0-9]+', '-', NAME.lower())}"
K = 5
try:     # Arm A reads the live stores exactly as the project's own config scopes them
    LIVE_CFG = json.loads((TARGET / ".ai-teammates" / "config.json").read_text()).get("memory") or {}
except (OSError, ValueError):
    LIVE_CFG = {}
BENCH_CFG = {"mempalace_wing": WING, "mempalace_palace": str(WORK / "palace"), "claude_mem_projects": [PROJECT]}
MEMPALACE = shutil.which("mempalace") or "mempalace"
CM_DB = Path.home() / ".claude-mem" / "claude-mem.db"


# --- corpus ----------------------------------------------------------------------------

def build_corpus() -> list:
    docs = []
    board = json.loads((TARGET / ".ai-teammates" / "board.json").read_text())
    for t in board["tasks"]:
        if len(t.get("note", "")) >= 40:
            docs.append({"slug": f"card-{t['id']}", "title": f"{t['id']}: {t['title']}",
                         "text": f"{NAME} board card {t['id']} ({t['status']}): {t['title']}\n{t['note']}"})
    rules = (TARGET / ".ai-teammates" / "RULES.md").read_text()
    sect = None
    for line in rules.splitlines():
        if line.strip() and not line[0].isspace():
            sect = {"slug": f"rules-{re.sub(r'[^a-z0-9]+', '-', line.lower()).strip('-')}",
                    "title": f"RULES.md: {line.strip().rstrip(':')}", "text": line + "\n"}
            docs.append(sect)
        elif sect is not None:
            sect["text"] += line + "\n"
    out = subprocess.run(["git", "-C", str(TARGET), "log", "--format=%h%x1f%ad%x1f%s%x1f%b%x1e", "--date=short"],
                         capture_output=True, text=True).stdout
    for rec in out.split("\x1e"):
        parts = rec.strip("\n").split("\x1f")
        if len(parts) == 4 and parts[0].strip():
            sha, day, subj, body = (p.strip() for p in parts)
            docs.append({"slug": f"commit-{sha}", "title": f"commit {sha}: {subj}",
                         "text": f"{NAME} git commit {sha} ({day})\n{subj}\n{body}".strip()})
    return docs


def load_questions() -> list:
    if not QUESTIONS.exists():
        sys.exit(f"no {QUESTIONS.name}: copy questions.example.json to {QUESTIONS} and write questions "
                 "whose answers your project's board, RULES.md or git history hold")
    return json.loads(QUESTIONS.read_text())["questions"]


def matches(text: str, pats) -> bool:
    return all(re.search(p, text, re.S) for p in pats)


def self_check(docs, questions) -> dict:
    """Which questions can this corpus answer at all? (and does it leak the abstention trap?)"""
    status = {}
    for q in questions:
        if q["type"] == "abstain":
            leak = [d["slug"] for d in docs if re.search(q["trap"], d["title"] + " " + d["text"])]
            status[q["id"]] = "ok" if not leak else f"INVALID: corpus mentions it in {leak[:2]}"
        else:
            n = sum(1 for d in docs if matches(d["title"] + "\n" + d["text"], q["must"]))
            status[q["id"]] = "ok" if n else "INVALID: no document in the corpus answers it"
    return status


# --- Arm B loading ------------------------------------------------------------------------

def cm_count() -> int:
    con = sqlite3.connect(f"file:{CM_DB}?mode=ro", uri=True, timeout=5)
    try:
        return con.execute("select count(*) from observations where project=?", (PROJECT,)).fetchone()[0]
    finally:
        con.close()


def load(docs) -> None:
    # MemPalace: a throwaway palace, so the real one is never touched
    shutil.rmtree(WORK, ignore_errors=True)
    (WORK / "corpus").mkdir(parents=True)
    for d in docs:
        (WORK / "corpus" / f"{d['slug']}.md").write_text(f"# {d['title']}\n\n{d['text']}\n")
    t0 = time.time()
    r = subprocess.run([MEMPALACE, "--palace", str(WORK / "palace"), "mine", str(WORK / "corpus"),
                        "--wing", WING, "--agent", "bench"], capture_output=True, text=True, timeout=3600)
    print(f"  mempalace: mined {len(docs)} documents in {time.time() - t0:.0f}s (exit {r.returncode})")
    if r.returncode != 0:
        sys.exit(r.stderr[-400:] or r.stdout[-400:])
    # claude-mem: the direct-save route (no LLM involved), under an isolated project name
    have = cm_count()
    if have not in (0, len(docs)):
        sys.exit(f"  claude-mem project '{PROJECT}' holds {have} of {len(docs)} documents; run `clean` first")
    if have == 0:
        base = memslice._cm_base()
        t0 = time.time()
        for i, d in enumerate(docs, 1):
            req = urllib.request.Request(f"{base}/api/memory/save", method="POST",
                                         data=json.dumps({"title": d["title"][:120], "text": d["text"],
                                                          "project": PROJECT}).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=60).read()
            if i % 100 == 0:
                print(f"    claude-mem: saved {i}/{len(docs)}")
        print(f"  claude-mem: saved {len(docs)} documents in {time.time() - t0:.0f}s")
    else:
        print(f"  claude-mem: {have} documents already loaded under '{PROJECT}'")
    time.sleep(5)               # let its vector sync catch up before anything is measured
    print(f"  claude-mem now holds {cm_count()} under '{PROJECT}'")


def clean() -> None:
    shutil.rmtree(WORK, ignore_errors=True)
    con = sqlite3.connect(CM_DB, timeout=10)
    n = con.execute("delete from observations where project=?", (PROJECT,)).rowcount
    con.execute("delete from sdk_sessions where project=?", (PROJECT,))
    con.commit()
    con.close()
    print(f"  removed the temp palace and {n} claude-mem rows. (Their vectors may linger in Chroma "
          f"unreferenced; they cannot be returned because the row is gone.)")


# --- running ---------------------------------------------------------------------------------

# Every system gets its best shot: claude-mem has two retrieval routes and is
# keyword-oriented, so prose questions alone would be an unfair way to judge it.
VARIANTS = [("mempalace", "mempalace", {}),
            ("claude-mem /search", "claude-mem", {"claude_mem_route": "search"}),
            ("claude-mem /semantic", "claude-mem", {"claude_mem_route": "semantic"})]
STYLES = (("prose", "q"), ("keywords", "kw"))


def run_arm(cfg: dict, questions, status, repeat: int) -> dict:
    res = {}
    for label, src, extra in VARIANTS:
        os.environ["MEMSLICE_CONFIG"] = json.dumps({**cfg, **extra})
        for style, field in STYLES:
            rows = []
            for q in questions:
                if not status[q["id"]].startswith("ok"):
                    continue
                lat, hits, idx = [], [], 0
                try:
                    for i in range(repeat):
                        hs, secs, idx = memslice.search(src, q[field], K)
                        lat.append(secs)
                        hits = hs if i == 0 else hits
                except memslice.MemError as e:
                    rows.append({"id": q["id"], "error": str(e)})
                    continue
                blocks = [memslice.est_tokens(memslice.block(h)) for h in hits]
                rank = None
                if q["type"] != "abstain":
                    for i, h in enumerate(hits, 1):
                        if matches(h["title"] + "\n" + h["text"], q["must"]):
                            rank = i
                            break
                rows.append({"id": q["id"], "type": q["type"], "rank": rank, "n": len(hits),
                             "tokens": sum(blocks), "to_first": sum(blocks[:rank]) if rank else sum(blocks),
                             "index_tokens": memslice.est_tokens("x" * idx) if idx and label.endswith("/search") else None,
                             "warm": lat[1:] or lat, "top": [h["id"] for h in hits[:3]]})
            res[f"{label} / {style}"] = rows
    return res


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))] if xs else float("nan")


def summarise(rows: list) -> dict:
    ans = [r for r in rows if r.get("type") not in (None, "abstain") and "error" not in r]
    errs = [r for r in rows if "error" in r]
    if not ans:
        return {"error": errs[0]["error"] if errs else "no answerable questions scored"}
    at = lambda k: sum(1 for r in ans if r["rank"] and r["rank"] <= k) / len(ans)
    lat = [x for r in rows if "warm" in r for x in r["warm"]]
    ab = [r for r in rows if r.get("type") == "abstain"]
    return {"n": len(ans), "hit1": at(1), "hit3": at(3), "hit5": at(5),
            "mrr": statistics.mean(1 / r["rank"] if r["rank"] else 0 for r in ans),
            "tokens": statistics.mean(r["tokens"] for r in ans),
            "to_first": statistics.mean(r["to_first"] for r in ans),
            "index_tokens": statistics.mean(r["index_tokens"] for r in ans if r["index_tokens"])
            if any(r["index_tokens"] for r in ans) else None,
            "p50": statistics.median(lat), "p95": pct(lat, 0.95),
            "abstain_tokens": ab[0]["tokens"] if ab else None, "errors": len(errs)}


def table(title, results, questions) -> str:
    out = [f"### {title}", "",
           "| system / query style | answered | hit@1 | hit@3 | hit@5 | MRR | tokens (5 hits) | tokens to 1st answer | p50 s | p95 s |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for key in results:
        s = summarise(results[key])
        if "error" in s:
            out.append(f"| {key} | ERROR: {s['error'][:90]} | | | | | | | | |")
            continue
        out.append(f"| {key} | {s['n']} q | {s['hit1']:.0%} | {s['hit3']:.0%} | {s['hit5']:.0%} | {s['mrr']:.2f} | "
                   f"{s['tokens']:.0f} | {s['to_first']:.0f} | {s['p50']:.2f} | {s['p95']:.2f} |")
    idxs = {k: summarise(v).get("index_tokens") for k, v in results.items()}
    idxs = {k: v for k, v in idxs.items() if v}
    if idxs:
        out.append("\nclaude-mem /search's cheap first step (titles only, before fetching), tokens: "
                   + ", ".join(f"{k.split(' / ')[1]} ~{v:.0f}" for k, v in idxs.items()))
    ab = {k: summarise(v).get("abstain_tokens") for k, v in results.items()}
    out.append("A question with NO answer still returns tokens (the cost of a useless lookup): "
               + ", ".join(f"{k} {v}" for k, v in ab.items()) + "\n")
    keys = list(results)
    short = lambda k: k.replace("claude-mem ", "cm").replace(" / ", " ").replace("mempalace", "mp")
    out += ["| question | " + " | ".join(short(k) for k in keys) + " |", "|---|" + "---|" * len(keys)]
    by = {k: {r["id"]: r for r in results[k]} for k in keys}
    for q in questions:
        def cell(k, q=q):
            r = by[k].get(q["id"])
            if r is None:
                return "-"
            if "error" in r:
                return "ERR"
            if q["type"] == "abstain":
                return "n/a"
            return f"**{r['rank']}**" if r["rank"] else "miss"
        out.append(f"| {q['id']} ({q['type']}) | " + " | ".join(cell(k) for k in keys) + " |")
    return "\n".join(out) + "\n"


def run(repeat: int) -> None:
    questions, docs = load_questions(), build_corpus()
    status = self_check(docs, questions)
    bad = {k: v for k, v in status.items() if v != "ok"}
    print(f"  corpus: {len(docs)} documents; questions: {len(questions)}; invalid: {len(bad)}")
    for k, v in bad.items():
        print(f"    {k}: {v}")
    md = [f"# MemPalace vs claude-mem on {NAME}: {date.today()}", "",
          f"Corpus: {len(docs)} documents (board cards, RULES.md sections, git history). {len(questions)} questions, "
          f"k={K}, each query repeated {repeat}x (first run discarded for latency). Queried through "
          "`memslice.py`, the adapter the CTO uses. A hit answers when ONE returned item contains every `must` pattern. "
          "Each system is scored with prose questions and with keyword queries; claude-mem with both of its routes "
          "(`/api/search` and the semantic route its own prompt hook uses).", ""]
    if bad:
        md += ["**INVALID questions (excluded, the corpus cannot answer them):** " + ", ".join(f"{k}: {v}" for k, v in bad.items()), ""]
    arms = [("Arm B: identical documents in both", BENCH_CFG, "Isolates retrieval quality from capture history.")]
    if LIVE_CFG.get("mempalace_wing") and LIVE_CFG.get("claude_mem_projects"):
        arms.insert(0, ("Arm A: live stores as they are today", LIVE_CFG,
                        "Includes whatever each system failed to capture."))
    else:
        print("  Arm A skipped: the project's config.json has no memory.mempalace_wing + claude_mem_projects")
    for name, cfg, note in arms:
        print(f"  running {name} ...")
        results = run_arm(cfg, questions, status, repeat)
        md += [f"## {name}", "", note, "", table(name, results, questions)]
        (HERE / "results").mkdir(exist_ok=True)
        (HERE / "results" / f"raw-{name[:5].replace(' ', '').replace(':', '').lower()}-{date.today()}.json").write_text(
            json.dumps(results, indent=1))
    out = HERE / "results" / f"{date.today()}-{WING[6:]}.md"
    out.write_text("\n".join(md))
    print(f"\n  wrote {out}\n")
    print("\n".join(md))


def native_probe(n: int = 25) -> None:
    """Fairness check for claude-mem: arm B feeds it raw documents, which is not what it is
    built to store. Here it is asked for its OWN observations, using each one's one-line
    description (subtitle, different words from the title) as the query. 'found' = that
    observation is among the top 5."""
    con = sqlite3.connect(f"file:{CM_DB}?mode=ro", uri=True, timeout=5)
    projects = LIVE_CFG.get("claude_mem_projects") or [NAME]
    rows = con.execute("select id, project, title, subtitle from observations where project in (%s) "
                       "and subtitle is not null and length(subtitle) > 30 and generated_by_model is not null "
                       "order by id desc limit ?" % ",".join("?" * len(projects)), (*projects, n)).fetchall()
    con.close()
    print(f"  {len(rows)} native {NAME} observations; query = its own subtitle; found = in top 5")
    for route in ("search", "semantic"):
        os.environ["MEMSLICE_CONFIG"] = json.dumps({"claude_mem_projects": sorted({r[1] for r in rows}),
                                                    "claude_mem_route": route})
        found = 0
        for oid, proj, title, sub in rows:
            hits, _, _ = memslice.search("claude-mem", sub, K)
            found += any(h["id"] == f"#{oid}" or h["title"].startswith(title[:40]) for h in hits)
        print(f"  claude-mem /{route:<9} found its own observation {found}/{len(rows)}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "load":
        load(build_corpus())
    elif cmd == "clean":
        clean()
    elif cmd == "run":
        run(int(sys.argv[sys.argv.index("--repeat") + 1]) if "--repeat" in sys.argv else 3)
    elif cmd == "native":
        native_probe()
    elif cmd == "check":
        d, q = build_corpus(), load_questions()
        print(f"  {len(d)} documents")
        for k, v in self_check(d, q).items():
            print(f"  {k:<18} {v}")
    else:
        print(__doc__)
