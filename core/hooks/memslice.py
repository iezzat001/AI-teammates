#!/usr/bin/env python3
"""memslice - the CTO decides which slice of memory travels with a card.

THE DESIGN (Ahmed, 2026-10-02): agents share ONE memory, but only the CTO decides
which part of it goes with a task. A seat is a narrow worker given one card; it
must not search memory itself, or it gets whatever the retrieval happens to
surface, at a cost nobody chose. Seats are launched with no MCP servers and none
of the user's plugins (cardrun), so they CANNOT reach either memory system. This
tool is the one door, and the CTO holds it.

One interface over both systems being compared, so the benchmark measures exactly
what a card would receive:

    memslice.py search "<query>" [--from mempalace|claude-mem] [--k 5] [--max-tokens 800] [--json]
    memslice.py attach <CARD-ID> "<query>" [--from S] [--pick 1,3] [--max-tokens 800]
    memslice.py show <CARD-ID>

`search` is the preview: it prints what `attach` would put on the card. `--pick`
keeps only the hits the CTO chose, by rank. The slice is capped (800 tokens, a
slice and not a dump) and stored verbatim on the card with its provenance, so a
card is reproducible and a reviewer can see what the seat was told.

What the slice guarantees about TRUTH (memlife.py): current facts that match the query come first;
superseded drawers are never returned; a line naming a retired term is tagged STALE; every hit says
how old it is. Those labels reach the seat inside the slice text.

Scoping is mandatory. config.json "memory":
    {"default": "mempalace",
     "mempalace_wing": "acme-notes",
     "claude_mem_projects": ["acme-notes", "acme-notes/acme-notes-dev"]}
Unset = refuse. An unfiltered MemPalace query surfaced another wing's memory as
the top hit on the pilot project (measured), and the personal wing shares that palace.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _config  # noqa: E402

SOURCES = ("mempalace", "claude-mem")
CHARS_PER_TOKEN = 4
MAX_SLICE_TOKENS = 800          # card.py caps the stored slice at 3200 chars = 800 tokens


class MemError(Exception):
    """Something the CTO has to act on, said plainly."""


def est_tokens(s: str) -> int:
    return (len(s) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def cfg() -> dict:
    c = {"default": "mempalace"}
    c.update(_config.load().get("memory") or {})
    try:                                       # the benchmark points each arm at its own store
        c.update(json.loads(os.environ.get("MEMSLICE_CONFIG", "") or "{}"))
    except ValueError:
        pass
    return c


# --- MemPalace ---------------------------------------------------------------------------

def _mempalace_python() -> str:
    """The interpreter the `mempalace` command itself runs on (it is not our python)."""
    exe = shutil.which("mempalace") or "/opt/homebrew/bin/mempalace"
    try:
        first = Path(exe).read_text(errors="ignore").splitlines()[0]
        if first.startswith("#!"):
            return first[2:].split()[0]
    except (OSError, IndexError):
        pass
    return sys.executable


def search_mempalace(query: str, k: int, room: str = "") -> list:
    """Current facts, then documents with the superseded room dropped (memlife owns both)."""
    import memlife
    return memlife.mempalace_hits(query, k, room)


# --- claude-mem ----------------------------------------------------------------------------

def _cm_base() -> str:
    c = cfg()
    if c.get("claude_mem_url"):
        return c["claude_mem_url"].rstrip("/")
    port = 37701
    try:
        s = json.loads((Path.home() / ".claude-mem" / "settings.json").read_text())
        port = int(s.get("CLAUDE_MEM_WORKER_PORT") or port)
    except Exception:
        pass
    return f"http://127.0.0.1:{port}"


def _get(url: str, timeout: float = 15):
    """One retry: the worker answers an empty body while it is busy syncing vectors
    (seen right after a bulk save). A single flake must not read as 'memory is down'."""
    last = None
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                body = r.read().decode("utf-8", errors="replace")
            if body.strip():
                return body
            last = "empty response"
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last = f"HTTP {e.code}"
        except Exception as e:
            last = type(e).__name__
        time.sleep(1.5)
    raise MemError(f"claude-mem worker did not answer at {_cm_base()} ({last}). "
                   "Is it running? `curl 127.0.0.1:37701/api/health`")


# rows of the index tables:  | #721 | 12:37 PM | 🔵 | Title | ~414 |
_ROW = re.compile(r"^\|\s*#(\d+)\s*\|[^|]*\|[^|]*\|\s*(.+?)\s*\|\s*~?\d*\s*\|\s*$", re.M)


def _post(url: str, payload: dict, timeout: float = 30):
    req = urllib.request.Request(url, method="POST", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    for _ in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8", errors="replace")
            if body.strip():
                return json.loads(body)
        except Exception:
            pass
        time.sleep(1.5)
    raise MemError(f"claude-mem worker did not answer the semantic route at {_cm_base()}")


_SEM_HEAD = re.compile(r"^### (.+?) \((\d{4}-\d{2}-\d{2})\)\s*$", re.M)


def _search_claude_mem_semantic(query: str, k: int, projects: list):
    """claude-mem's semantic route (POST /api/context/semantic): the one its own prompt
    hook uses to inject memories. It returns one text block, parsed back into hits."""
    hits, chars = [], 0
    for proj in projects:
        ctx = (_post(f"{_cm_base()}/api/context/semantic", {"q": query, "project": proj, "limit": k})
               .get("context") or "")
        chars += len(ctx)
        parts = _SEM_HEAD.split(ctx)           # [pre, title, date, body, title, date, body ...]
        for i in range(1, len(parts) - 2, 3):
            title, day, body = parts[i], parts[i + 1], parts[i + 2].strip()
            hits.append({"source": "claude-mem", "id": f"sem:{proj}:{title[:50]}", "title": title,
                         "text": body, "score": None, "date": day})
    return hits[:k], chars


def search_claude_mem(query: str, k: int, index_only: bool = False):
    c = cfg()
    projects = c.get("claude_mem_projects") or ([c["claude_mem_project"]] if c.get("claude_mem_project") else [])
    if not projects:
        raise MemError("memory.claude_mem_projects is not set in .ai-teammates/config.json "
                       "(claude-mem is scoped per project; unscoped would mix every project you work on).")
    if c.get("claude_mem_route") == "semantic":
        return _search_claude_mem_semantic(query, k, projects)
    rows, seen, index_chars = [], set(), 0
    for proj in projects:
        body = _get(f"{_cm_base()}/api/search?query={urllib.parse.quote(query)}"
                    f"&project={urllib.parse.quote(proj)}&limit={k}")
        if not body:
            continue
        text = (json.loads(body).get("content") or [{}])[0].get("text", "")
        index_chars += len(text)
        for oid, title in _ROW.findall(text):
            if oid not in seen:
                seen.add(oid)
                rows.append((oid, title))
    rows = rows[:k]
    if index_only:
        return [{"source": "claude-mem", "id": f"#{o}", "title": t, "text": "", "score": None, "date": ""}
                for o, t in rows], index_chars
    hits = []
    for oid, title in rows:
        raw = _get(f"{_cm_base()}/api/observation/{oid}")
        if not raw:
            continue
        o = json.loads(raw)
        try:
            facts = json.loads(o.get("facts") or "[]")
        except ValueError:
            facts = []
        text = "\n".join(x for x in [o.get("subtitle") or "", o.get("narrative") or "",
                                     "\n".join(f"- {f}" for f in facts)] if x)
        hits.append({"source": "claude-mem", "id": f"#{oid}", "title": o.get("title") or title, "text": text,
                     "score": None, "date": (o.get("created_at") or "")[:10]})
    return hits, index_chars


def search(source: str, query: str, k: int = 5, room: str = ""):
    """-> (hits, seconds, index_chars). index_chars is what claude-mem's cheap first
    step returned before the fetch (0 for MemPalace, which has no index step)."""
    t0 = time.time()
    if source == "mempalace":
        hits, idx = search_mempalace(query, k, room), 0
    elif source == "claude-mem":
        hits, idx = search_claude_mem(query, k)
    else:
        raise MemError(f"unknown memory source '{source}' (use one of: {', '.join(SOURCES)})")
    import memlife
    return memlife.shape(query, hits), round(time.time() - t0, 3), idx


# --- the slice -------------------------------------------------------------------------------

def block(h: dict) -> str:
    import memlife
    when = f" {h['date']}" if h.get("date") else ""
    flags = memlife.label(h)
    flags = f" ({flags})" if flags else ""
    return f"[{h['source']} {h['id']}{when}{flags}] {h['title']}\n{h['text'].strip()}"


CUT = " ...[cut]"


def fit(hits: list, max_tokens: int):
    """Greedy by rank until the budget is used; the last hit is cut, not dropped.
    -> (kept_hits, text, tokens).

    Budgeted in characters of the FINAL text, separators and the cut mark included. FOUND 2026-10-28
    (repo analysis): counting tokens per hit missed both, so a slice reached 3216 chars and the card
    it had just been attached to failed card.py check (max 3200)."""
    limit = max_tokens * CHARS_PER_TOKEN
    kept, parts, size = [], [], 0
    for h in hits:
        b = block(h)
        sep = 2 if parts else 0                      # the blank line between hits
        if size + sep + len(b) <= limit:
            kept.append(h); parts.append(b); size += sep + len(b)
            continue
        room = limit - size - sep - len(CUT)
        if room > 240:
            kept.append(h); parts.append(b[:room].rstrip() + CUT)
        break
    text = "\n\n".join(parts)
    return kept, text, est_tokens(text)


def _pick(hits: list, spec: str) -> list:
    if not spec:
        return hits
    want = [int(x) for x in re.split(r"[,\s]+", spec.strip()) if x.isdigit()]
    return [hits[i - 1] for i in want if 0 < i <= len(hits)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="memslice.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("search", "attach"):
        p = sub.add_parser(name)
        if name == "attach":
            p.add_argument("card")
        p.add_argument("query")
        p.add_argument("--from", dest="src", default="")
        p.add_argument("--k", type=int, default=5)
        p.add_argument("--max-tokens", type=int, default=MAX_SLICE_TOKENS)
        p.add_argument("--room", default="")
        p.add_argument("--pick", default="")
        p.add_argument("--json", action="store_true")
    sub.add_parser("show").add_argument("card")
    a = ap.parse_args(argv)

    try:
        if a.cmd == "show":
            import card as cardmod
            m = (cardmod.load(a.card).get("memory") or {})
            if not m.get("text"):
                print(f"  card {a.card} has no memory slice")
                return 0
            print(f"  from {m.get('source')} · query {m.get('query')!r} · ~{m.get('tokens', '?')} tokens · {m.get('attached', '')}")
            print(f"  ids: {', '.join(m.get('ids') or [])}\n\n{m['text']}")
            return 0

        src = a.src or cfg().get("default") or "mempalace"
        cap = min(a.max_tokens, MAX_SLICE_TOKENS)
        hits, secs, _ = search(src, a.query, a.k, a.room)
        chosen = _pick(hits, a.pick)
        kept, text, used = fit(chosen, cap)
        if a.json:
            print(json.dumps({"source": src, "query": a.query, "seconds": secs, "tokens": used,
                              "hits": [{**h, "text": h["text"][:400]} for h in hits],
                              "kept": [h["id"] for h in kept]}, ensure_ascii=False, indent=1))
            return 0
        print(f"  {src} · {len(hits)} hit(s) in {secs}s · slice would be ~{used} of {cap} tokens\n")
        for i, h in enumerate(hits, 1):
            mark = "*" if h in kept else " "
            sc = f" score {h['score']}" if h.get("score") is not None else ""
            first = " ".join(h["text"].split())[:110]
            import memlife
            fl = memlife.label(h)
            fl = f"  <{fl}>" if fl else ""
            print(f" {mark}[{i}] {h['id']}  {h['title'][:60]}{sc}{fl}  ~{est_tokens(block(h))} tok\n       {first}")
        if a.cmd == "search":
            print("\n  * = fits the slice. Narrow with --pick 1,3 ; put it on a card with: "
                  "memslice.py attach <CARD> \"<query>\" --pick ...")
            return 0
        import card as cardmod
        c = cardmod.load(a.card)
        c["memory"] = {"source": src, "query": a.query, "ids": [h["id"] for h in kept], "text": text,
                       "tokens": used, "attached": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        cardmod.save(c)
        print(f"\n  attached {len(kept)} hit(s), ~{used} tokens, to card {a.card}")
        return 0
    except MemError as e:
        print(f"  memslice: {e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"  memslice: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
