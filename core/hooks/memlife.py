#!/usr/bin/env python3
"""memlife - keep memory TRUE, not just full.

THE DEFECT (Ahmed, 2026-10-03): a project evolves, and memory only ever grows. Search ranks by
similarity, so a retired decision that is phrased well outranks the current one. Measured on
The pilot project: the slice built for a card still told the seat the UI seat was "opencode +
glm-5.3-flash" - one stale line inside an otherwise correct drawer - and one drawer holds both
the dead rule and its replacement. Deleting is not the answer (the history is real and sometimes
the question), and re-ranking on age alone buries a decision that is old and still right.
So four small mechanisms, each visible to whoever reads the slice:

  1. FACTS      one CURRENT statement per topic ("production host"). Setting a new one retires the
                old, which is kept, dated, and never returned by a search. Facts that match the
                query lead every slice, ahead of the documents that happen to be similar.
  2. TOMBSTONES `memory.retired` in config.json: terms that are no longer true. A hit that names
                one has that LINE tagged STALE in the slice (not the whole drawer: the rest is
                usually fine), unless the query itself names the term (then you are asking about
                the history, and the old text is the answer).
  3. SUPERSEDE  move one drawer to room `superseded`, labelled with what replaced it. Search drops
                that room. `restore` undoes it.
  4. FILE       when a card is merged, its outcome becomes a decision record in room `cards`
                (idempotent), so the memory grows from what was actually delivered, not from
                what somebody remembered to write down. `audit` lists what still names a
                retired term, and the facts nobody has re-confirmed lately.

  5. REVIEW     what changed gets checked against the standing facts, by a hook rather than by
                anyone's memory (2026-10-06: a week of work changed how Gemini is reached and no fact
                was recorded). When a CTO turn ends and commits reached the integration branch in
                this session that no fact review has covered, a Stop hook asks once: record what is
                now true, or say nothing standing changed. The next session's pre-flight lists
                anything still unreviewed. Board-only commits and merges are not counted.

    memlife.py fact set "<topic>" "<statement>"      replace the current statement for a topic
    memlife.py fact retire "<topic>" [--why ...]     it is no longer true, and nothing replaces it
    memlife.py fact ls [--all]                       current facts (--all adds the retired ones)
    memlife.py retire-term "<term>" --why "<what replaced it>"
    memlife.py supersede <hit-id|drawer-id> [--by "<text>"] [--why ...]
    memlife.py restore <hit-id|drawer-id>
    memlife.py file <CARD>                           record a done card as a decision
    memlife.py audit                                 read-only report
    memlife.py fact reviewed --why "<one line>"      nothing standing changed (marks the review done)
    memlife.py pending                               pre-flight: commits since the last fact review

Every write command takes --dry-run (print what would change, change nothing). The store is the
MemPalace wing in config.json `memory.mempalace_wing`; claude-mem gets the tombstone and age
labels on what it returns, but has no facts or supersede (it is not ours to rewrite).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _config  # noqa: E402
import memslice  # noqa: E402

FACT_ROOM, OLD_ROOM, CARD_ROOM = "facts", "superseded", "cards"
DEFAULTS = {"stale_after_days": 60,        # a hit filed longer ago than this is marked AGED
            "fact_min_similarity": 0.2,    # a fact below this is not about the query
            "fact_review_days": 90,        # `audit` asks for a fact to be re-confirmed after this
            "stale_fraction": 0.4,         # a hit with this share of retired lines sinks below the rest
            "facts_per_slice": 3}
# a line that already says the old thing is old needs no tag from us
MARKERS = re.compile(r"supersed|no longer|retired|dropped|removed|replaced|\bdead\b|deprecat|"
                     r"do not follow|historical|obsolete|was tried|ditch", re.I)
SUPERSEDED_PREFIX = re.compile(r"^SUPERSEDED (\d{4}-\d{2}-\d{2}) \[was: ([^\]]+)\][^\n]*\n---\n", re.S)


def conf() -> dict:
    return {**DEFAULTS, **memslice.cfg()}


def today() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")      # the owner's calendar, not UTC's


# --- the bridge to MemPalace (it lives on its own interpreter) ------------------------------------

_BRIDGE = r'''
import json, os, sys
from pathlib import Path
a = json.load(sys.stdin)
os.environ["MEMPALACE_PALACE_PATH"] = a["palace"]
import mempalace.mcp_server as ms
if a.get("wal"):                      # a test palace keeps its own write log, not the user's
    ms._WAL_FILE = Path(a["wal"])
op = a["op"]
if op == "search":
    from mempalace.searcher import search_memories
    out = [search_memories(q["q"], a["palace"], wing=a["wing"], room=q.get("room"), n_results=q["n"])
           for q in a["queries"]]
elif op == "list":
    c = ms._get_collection()
    out = []
    if c is not None:
        got = c.get(where={"wing": a["wing"]}, include=["documents", "metadatas"])
        out = [{"id": i, "doc": d, "meta": m} for i, d, m in zip(got["ids"], got["documents"], got["metadatas"])]
elif op == "add":
    out = ms.tool_add_drawer(a["wing"], a["room"], a["content"], source_file=a.get("source_file"),
                             added_by=a.get("by", "memlife"))
elif op == "update":
    out = ms.tool_update_drawer(a["id"], content=a.get("content"), room=a.get("room"))
else:
    out = {"success": False, "error": "unknown op " + op}
# importing mcp_server redirects stdout to stderr (an MCP server must keep stdout clean for the
# protocol), so the answer goes to a file the caller named, not to a pipe
Path(a["out"]).write_text(json.dumps(out, default=str))
'''


def _palace(c: dict) -> str:
    return os.path.expanduser(c.get("mempalace_palace") or "~/.mempalace/palace")


def _wing(c: dict) -> str:
    wing = c.get("mempalace_wing")
    if not wing:
        raise memslice.MemError("memory.mempalace_wing is not set in .ai-teammates/config.json. An unfiltered "
                                "MemPalace query returns other projects' memories, so this refuses rather than guess.")
    return wing


def bridge(op: str, c: dict = None, **args):
    c = c or conf()
    palace = _palace(c)
    payload = {"op": op, "palace": palace, "wing": _wing(c), **args}
    if os.path.abspath(palace) != os.path.abspath(os.path.expanduser("~/.mempalace/palace")):
        payload["wal"] = palace.rstrip("/") + "-wal.jsonl"
    fd, out = tempfile.mkstemp(prefix="memlife-", suffix=".json")
    os.close(fd)
    payload["out"] = out
    try:
        p = subprocess.run([memslice._mempalace_python(), "-c", _BRIDGE], capture_output=True, text=True,
                           timeout=180, input=json.dumps(payload))
        body = Path(out).read_text()
    except (OSError, subprocess.TimeoutExpired) as e:
        raise memslice.MemError(f"MemPalace could not run ({type(e).__name__}).")
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass
    if p.returncode != 0 or not body.strip():
        raise memslice.MemError("MemPalace failed: " + (p.stderr.strip().splitlines() or ["no output"])[-1][:160])
    return json.loads(body)


def _must(r: dict, what: str) -> dict:
    if not (r or {}).get("success"):
        raise memslice.MemError(f"{what} failed: {(r or {}).get('error') or r}")
    return r


# --- what a search returns: facts first, the dead dropped, the stale tagged ---------------------------

def _words(s: str) -> set:
    return set(re.findall(r"[a-z0-9]+", s.lower()))


def names_term(query: str, term: str) -> bool:
    """The query is ABOUT the retired thing, so its old text is the answer, not a trap."""
    q, t = query.lower(), term.lower().strip()
    if not t:
        return False
    if t in q:
        return True
    sig = [w for w in _words(t) if len(w) >= 3]
    return bool(sig) and all(w in _words(q) for w in sig)


def _term_re(term: str):
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(term.strip()) + r"(?![A-Za-z0-9])", re.I)


def stale_lines(text: str, terms: list) -> list:
    """[(line_no, line, term)] for lines that name a retired term without saying it is old."""
    out = []
    res = [(t, _term_re(t["term"])) for t in terms if (t.get("term") or "").strip()]
    for n, line in enumerate(text.splitlines()):
        if MARKERS.search(line):
            continue
        for t, rx in res:
            if rx.search(line):
                out.append((n, line, t))
                break
    return out


def tag(text: str, query: str, terms: list):
    """-> (tagged_text, n_tagged, n_lines). Skips terms the query names."""
    live = [t for t in terms if not names_term(query, t.get("term", ""))]
    hit = {n: t for n, _, t in stale_lines(text, live)}
    out = []
    for n, line in enumerate(text.splitlines()):
        if n in hit:
            why = (hit[n].get("why") or "").strip()[:90]
            line += f"   <- STALE: '{hit[n]['term']}' is retired" + (f" ({why})" if why else "")
        out.append(line)
    nonblank = sum(1 for l in text.splitlines() if l.strip())
    return "\n".join(out), len(hit), nonblank


def age_days(date: str):
    try:
        return max(0, (datetime.now(timezone.utc).date() - datetime.strptime(date[:10], "%Y-%m-%d").date()).days)
    except (ValueError, TypeError):
        return None


def shape(query: str, hits: list, c: dict = None) -> list:
    """Label every hit with its age and its stale lines, then order: facts, clean documents,
    documents that are mostly about retired things. Ordering inside each group is the search's."""
    c = c or conf()
    terms = c.get("retired") or []
    for h in hits:
        if h.get("kind") != "fact":
            text, n, total = tag(h.get("text") or "", query, terms)
            h["text"], h["stale_lines"] = text, n
            h["mostly_stale"] = bool(n and total and n / total >= c["stale_fraction"])
        d = age_days(h.get("date") or "")
        h["age"] = d
        h["aged"] = d is not None and h.get("kind") != "fact" and d > c["stale_after_days"]
    facts = [h for h in hits if h.get("kind") == "fact"]
    docs = [h for h in hits if h.get("kind") != "fact"]
    return facts + [h for h in docs if not h["mostly_stale"]] + [h for h in docs if h["mostly_stale"]]


def label(h: dict) -> str:
    """The flags the slice shows beside a hit."""
    if h.get("kind") == "fact":
        return "CURRENT FACT"
    bits = []
    if h.get("age") is not None:
        bits.append(f"{h['age']}d old")
    if h.get("aged"):
        bits.append("AGED")
    if h.get("mostly_stale"):
        bits.append("MOSTLY ABOUT RETIRED THINGS")
    elif h.get("stale_lines"):
        bits.append(f"{h['stale_lines']} STALE line(s)")
    return " · ".join(bits)


# --- search, as memslice uses it ------------------------------------------------------------------------

def mempalace_hits(query: str, k: int, room: str = "") -> list:
    """Documents (the dead room filtered out) behind the current facts that match the query."""
    c = conf()
    queries = [{"q": query, "room": room or None, "n": k + 8}]
    if not room:
        queries.append({"q": query, "room": FACT_ROOM, "n": c["facts_per_slice"]})
    res = bridge("search", c, queries=queries)
    for r in res:
        if r.get("error"):
            raise memslice.MemError(f"MemPalace: {r['error']}")
    hits = []
    for r in res[0].get("results") or []:
        if r.get("room") in (OLD_ROOM, FACT_ROOM) and not room:
            continue
        hits.append({"source": "mempalace", "kind": "doc",
                     "id": f"{r.get('wing')}/{r.get('room')}/{r.get('source_file')}#{r.get('drawer_index', 0)}",
                     "title": r.get("source_file") or "", "text": r.get("text") or "",
                     "score": r.get("similarity"), "date": (r.get("created_at") or "")[:10]})
    hits = hits[:k]
    facts = []
    if len(res) > 1:
        for r in res[1].get("results") or []:
            if (r.get("similarity") or 0) >= c["fact_min_similarity"]:
                topic, statement, since = parse_fact(r.get("text") or "")
                facts.append({"source": "mempalace", "kind": "fact", "id": f"fact:{topic}", "title": topic,
                              "text": statement, "score": r.get("similarity"), "date": since or (r.get("created_at") or "")[:10]})
    return facts + hits


# --- facts ---------------------------------------------------------------------------------------------------

def parse_fact(doc: str):
    lines = doc.strip().splitlines()
    if not lines or not lines[0].startswith("FACT | "):
        return "", doc.strip(), ""
    since = ""
    body = []
    for l in lines[1:]:
        m = re.match(r"^since (\d{4}-\d{2}-\d{2})", l)
        if m:
            since = m.group(1)
        else:
            body.append(l)
    return lines[0][len("FACT | "):].strip(), "\n".join(body).strip(), since


def render_fact(topic: str, statement: str, when: str) -> str:
    return f"FACT | {topic.strip()}\n{statement.strip()}\nsince {when}"


def _drawers(c=None) -> list:
    return bridge("list", c)


def current_facts(c=None) -> list:
    out = []
    for d in _drawers(c):
        if d["meta"].get("room") == FACT_ROOM and d["doc"].startswith("FACT | "):
            topic, statement, since = parse_fact(d["doc"])
            out.append({"id": d["id"], "topic": topic, "statement": statement, "since": since})
    return sorted(out, key=lambda f: f["topic"].lower())


def retired_drawers(c=None) -> list:
    out = []
    for d in _drawers(c):
        if d["meta"].get("room") == OLD_ROOM:
            m = SUPERSEDED_PREFIX.match(d["doc"])
            out.append({"id": d["id"], "when": m.group(1) if m else "", "was": m.group(2) if m else "",
                        "doc": d["doc"], "src": d["meta"].get("source_file", "")})
    return out


def _retire_drawer(drawer: dict, was_room: str, why: str, dry: bool, c=None):
    doc = drawer["doc"]
    if SUPERSEDED_PREFIX.match(doc):
        return
    new = f"SUPERSEDED {today()} [was: {was_room}] - {why or 'no longer true'}\n---\n{doc}"
    if not dry:
        _must(bridge("update", c, id=drawer["id"], content=new, room=OLD_ROOM), "moving the old drawer")


def fact_set(topic: str, statement: str, dry: bool = False) -> str:
    c = conf()
    if len(statement.strip()) > 600:
        raise memslice.MemError("a fact is one short statement (600 characters at most). Put the story in a "
                                "document; keep the fact to what is true now.")
    cur = [d for d in _drawers(c) if d["meta"].get("room") == FACT_ROOM and d["doc"].startswith("FACT | ")
           and parse_fact(d["doc"])[0].lower() == topic.strip().lower()]
    for d in cur:
        _, said, since = parse_fact(d["doc"])
        if said == statement.strip():
            # Setting it again re-confirms it: the date moves, so `audit` stops asking. FOUND 2026-10-28
            # (repo analysis): this returned without writing, and the 90-day reminder could never clear.
            if since == today():
                return f"  already current: {topic}"
            if not dry:
                _must(bridge("update", c, id=d["id"], content=render_fact(topic, said, today())), "renewing the fact")
                review_mark(f"fact renewed: {topic.strip()}")
            return f"  already current: {topic}; {'would renew' if dry else 'renewed'} (since {today()}, was {since})"
    for d in cur:
        _retire_drawer(d, FACT_ROOM, f"replaced on {today()} by a newer statement for '{topic}'", dry, c)
    if not dry:
        _must(bridge("add", c, room=FACT_ROOM, content=render_fact(topic, statement, today()),
                     source_file=f"fact:{topic.strip()}", by="memlife"), "filing the fact")
        review_mark(f"fact set: {topic.strip()}")
    return (f"  {'would set' if dry else 'set'} fact '{topic}': {statement.strip()}\n"
            + (f"  {'would retire' if dry else 'retired'} {len(cur)} earlier statement(s) (kept in room {OLD_ROOM})"
               if cur else "  (first statement for this topic)"))


def fact_retire(topic: str, why: str, dry: bool = False) -> str:
    c = conf()
    cur = [d for d in _drawers(c) if d["meta"].get("room") == FACT_ROOM and d["doc"].startswith("FACT | ")
           and parse_fact(d["doc"])[0].lower() == topic.strip().lower()]
    if not cur:
        raise memslice.MemError(f"no current fact named '{topic}' (see: memlife.py fact ls)")
    for d in cur:
        _retire_drawer(d, FACT_ROOM, why or f"retired on {today()}", dry, c)
    if not dry:
        review_mark(f"fact retired: {topic.strip()}")
    return f"  {'would retire' if dry else 'retired'} fact '{topic}' ({len(cur)} drawer)"


# --- supersede / restore ------------------------------------------------------------------------------------------

def resolve(sel: str, c=None) -> dict:
    """A raw drawer id, or the id a search printed: wing/room/source_file#chunk."""
    drawers = _drawers(c)
    for d in drawers:
        if d["id"] == sel:
            return d
    m = re.match(r"^([^/]+)/([^/]+)/(.+?)(?:#(\d+))?$", sel)
    if m:
        wing, room, src, idx = m.group(1), m.group(2), m.group(3), int(m.group(4) or 0)
        def lived_in(d):     # a superseded drawer is still addressed by the room it came from
            was = SUPERSEDED_PREFIX.match(d["doc"])
            return d["meta"].get("room") == room or (d["meta"].get("room") == OLD_ROOM and was and was.group(2) == room)
        same = sorted((d for d in drawers if lived_in(d) and d["meta"].get("source_file") == src),
                      key=lambda d: d["meta"].get("chunk_index", 0))
        if same:
            return same[min(idx, len(same) - 1)]
    raise memslice.MemError(f"no drawer matches '{sel}' in wing {_wing(c or conf())}. Use the id a search printed "
                            "(wing/room/file#n) or a drawer id from `memlife.py audit`.")


def supersede(sel: str, by: str, why: str, dry: bool = False) -> str:
    c = conf()
    d = resolve(sel, c)
    room = d["meta"].get("room")
    if room == OLD_ROOM:
        return f"  already superseded: {sel}"
    reason = why or (f"replaced by: {by}" if by else "no longer true")
    if why and by:
        reason = f"{why} (replaced by: {by})"
    _retire_drawer(d, room, reason, dry, c)
    return (f"  {'would supersede' if dry else 'superseded'} {d['id']} ({room}/{d['meta'].get('source_file')}): "
            f"{reason}\n  search no longer returns it; undo with: memlife.py restore {d['id']}")


def restore(sel: str, dry: bool = False) -> str:
    c = conf()
    d = resolve(sel, c)
    m = SUPERSEDED_PREFIX.match(d["doc"])
    if d["meta"].get("room") != OLD_ROOM or not m:
        return f"  {d['id']} is not superseded"
    if not dry:
        _must(bridge("update", c, id=d["id"], content=d["doc"][m.end():], room=m.group(2)), "restoring")
    return f"  {'would restore' if dry else 'restored'} {d['id']} to room {m.group(2)}"


# --- tombstones ------------------------------------------------------------------------------------------------------------

def retire_term(term: str, why: str, dry: bool = False) -> str:
    path = _config.repo_root() / ".ai-teammates" / "config.json"
    try:
        cfgd = json.loads(path.read_text())
    except (OSError, ValueError):
        raise memslice.MemError(f"{path} is missing or unreadable")
    mem = cfgd.setdefault("memory", {})
    terms = mem.setdefault("retired", [])
    if any(t.get("term", "").lower() == term.strip().lower() for t in terms):
        return f"  '{term}' is already retired"
    terms.append({"term": term.strip(), "why": why.strip()})
    if not dry:
        path.write_text(json.dumps(cfgd, indent=2, ensure_ascii=False) + "\n")
    return f"  {'would retire' if dry else 'retired'} the term '{term.strip()}' in {path.name} ({len(terms)} now)"


# --- file a merged card as a decision ----------------------------------------------------------------------------------------

def last_run(cid: str):
    f = _config.state_dir() / "agent_runs.jsonl"
    row = None
    try:
        for line in f.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("card") == cid:
                row = r
    except OSError:
        pass
    return row


def draft_record(cid: str) -> str:
    import card as cardmod
    c = cardmod.load(cid)
    r = last_run(cid)
    if not r or r.get("status") != "done":
        raise memslice.MemError(f"card {cid} has no finished run to record (last status: "
                                f"{(r or {}).get('status', 'never ran')}). Only a done card is a decision.")
    files = r.get("files") or []
    checks = "; ".join(a.get("why") or a.get("run", "")[:60] for a in (c.get("acceptance") or []))
    return "\n".join([
        f"CARD {cid} done {str(r.get('ts', ''))[:10]} - {c.get('title') or c.get('goal', '')[:80]}",
        f"Goal: {c.get('goal', '')}",
        f"Changed: {', '.join(files) or 'see branch'} ({r.get('diffstat') or 'no diffstat'}) "
        f"on {r.get('branch')} @{r.get('head')}",
        f"Verified by: {checks or 'its acceptance checks'}",
        f"Delivered by {r.get('agent')} on {r.get('model')} in {r.get('seconds')}s, ${float(r.get('cost_usd') or 0):.3f}",
        "If this changed what is true now (a tool, a host, a rule), record it: "
        "memlife.py fact set \"<topic>\" \"<what is true now>\""])


def file_card(cid: str, dry: bool = False) -> str:
    content = draft_record(cid)
    if dry:
        return "  would file to room cards:\n" + "\n".join("    " + l for l in content.splitlines())
    r = _must(bridge("add", None, room=CARD_ROOM, content=content, source_file=f"card:{cid}", by="memlife"),
              "filing the card")
    return f"  filed card {cid} as a decision" + (" (it already was)" if r.get("reason") == "already_exists" else "")


# --- audit --------------------------------------------------------------------------------------------------------------------------

def audit() -> str:
    c = conf()
    terms = c.get("retired") or []
    drawers = _drawers(c)
    live = [d for d in drawers if d["meta"].get("room") not in (OLD_ROOM, FACT_ROOM)]
    out = [f"  wing {_wing(c)}: {len(live)} documents, "
           f"{sum(1 for d in drawers if d['meta'].get('room') == FACT_ROOM)} facts, "
           f"{sum(1 for d in drawers if d['meta'].get('room') == OLD_ROOM)} superseded, "
           f"{len(terms)} retired terms"]
    if not terms:
        out.append("  no retired terms yet: nothing to check documents against. "
                   "Add one: memlife.py retire-term \"<term>\" --why \"<what replaced it>\"")
    rows = []
    for d in live:
        sl = stale_lines(d["doc"], terms)
        if sl:
            rows.append((len(sl), d, sl[0]))
    if terms:
        out.append(f"\n  {len(rows)} document(s) still name a retired term without saying it is old:")
        for n, d, (ln, line, t) in sorted(rows, key=lambda r: -r[0])[:15]:
            out.append(f"   {n:>2} line(s)  {d['meta'].get('room')}/{d['meta'].get('source_file')}  "
                       f"[{d['id']}]\n              '{t['term']}': {line.strip()[:100]}")
        if rows:
            out.append("   fix: supersede the drawer if all of it is dead, otherwise leave it; "
                       "searches already tag these lines STALE")
    old = [f for f in current_facts(c) if (age_days(f["since"]) or 0) > c["fact_review_days"]]
    if old:
        out.append(f"\n  {len(old)} fact(s) not re-confirmed for {c['fact_review_days']} days "
                   "(still true? `fact set` again to renew, or `fact retire`):")
        out += [f"   {f['topic']} (since {f['since']}): {f['statement'][:90]}" for f in old]
    return "\n".join(out)


# --- review: what changed is checked against the standing facts --------------------------------------------

ROUTINE = (".ai-teammates/board.json", ".ai-teammates/cards/")      # bookkeeping, not a change of truth


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _git(*args):
    return subprocess.run([_config.GIT, *args], cwd=str(_config.repo_root()), capture_output=True, text=True)


def _branch() -> str:
    """The branch whose history IS what is true: integration if the project has one."""
    production, integration = _config.branches()
    return integration or production


def _tip() -> str:
    r = _git("rev-parse", "--verify", "--quiet", _branch() + "^{commit}")
    return r.stdout.strip() if r.returncode == 0 else ""


def _review_dir() -> Path:
    d = _config.state_dir() / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _jread(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def _jwrite(p: Path, d: dict):
    p.write_text(json.dumps(d, indent=1))


def _marker() -> Path:
    return _review_dir() / "fact-review.json"


def _session_file(sid: str) -> Path:
    return _review_dir() / "sessions" / (re.sub(r"[^A-Za-z0-9_.-]", "_", sid)[:80] + ".json")


def review_mark(why: str) -> str:
    """Facts have been checked against everything on the integration branch up to now."""
    tip = _tip()
    if tip:
        _jwrite(_marker(), {"through": tip, "at": _now(), "why": why[:200]})
    return tip


def review_init():
    """The first time, history counts as reviewed: only what comes after is asked about."""
    if not _marker().exists():
        review_mark("fact review started")


def unreviewed(exclude) -> list:
    """[(short sha, subject)] on the integration branch, newest first, not reachable from any sha in
    `exclude`. Merges are left out (their commits are counted themselves), and so are commits that
    only touch the board or card files."""
    tip = _tip()
    if not tip:
        return []
    ex = [x for x in exclude if x and _git("cat-file", "-e", x + "^{commit}").returncode == 0]
    r = _git("log", "--no-merges", "--format=%x01%h%x02%s", "--name-only", tip, *("^" + x for x in ex))
    out = []
    for block in r.stdout.split("\x01")[1:]:
        head, _, files = block.partition("\n")
        sha, _, subject = head.partition("\x02")
        fs = [f.strip() for f in files.splitlines() if f.strip()]
        if fs and all(f == ROUTINE[0] or f.startswith(ROUTINE[1]) for f in fs):
            continue
        out.append((sha, subject))
    return out


def _review_on(c: dict) -> bool:
    return bool(c.get("mempalace_wing")) and c.get("fact_review", True) is not False


def _is_seat() -> bool:
    return bool(os.environ.get("AI_TEAMMATES_SEAT") or os.environ.get("AI_TEAMMATES_CARD"))


def session_begin(sid: str):
    """SessionStart: remember where the integration branch stood, so a CTO turn is asked only about
    what THIS session landed. Seats keep no baseline (they never record facts)."""
    if not sid or _is_seat() or not _review_on(conf()):
        return
    review_init()
    if os.environ.get("CLAUDE_AGENT_NAME") != "cto":
        return
    f = _session_file(sid)
    f.parent.mkdir(exist_ok=True)
    if not f.exists():                         # a resumed or compacted session keeps its baseline
        _jwrite(f, {"through": _tip(), "at": _now()})
    cutoff = datetime.now().timestamp() - 30 * 86400
    for old in f.parent.glob("*.json"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass


def stop_hook(payload: dict) -> dict:
    """Stop: {} lets the turn end; a block makes the CTO review the facts first. Asked once per state
    of the integration branch, never twice in a row (stop_hook_active), never in a seat."""
    if os.environ.get("CLAUDE_AGENT_NAME") != "cto" or _is_seat() or payload.get("stop_hook_active"):
        return {}
    if not _review_on(conf()):
        return {}
    f = _session_file(str(payload.get("session_id") or ""))
    base = _jread(f)
    tip = _tip()
    if not base.get("through") or not tip or base.get("asked") == tip:
        return {}
    todo = unreviewed([base["through"], _jread(_marker()).get("through", "")])
    if not todo:
        return {}
    base["asked"] = tip
    _jwrite(f, base)
    shown = "\n".join(f"  {sha} {subject[:100]}" for sha, subject in todo[:6])
    more = f"\n  ...and {len(todo) - 6} more" if len(todo) > 6 else ""
    tail = ("This is a headless run: after that, repeat your final report to the owner in full, because "
            "your last message is the only one they will read." if os.environ.get("AI_TEAMMATES_HEADLESS")
            else "Then finish with one line saying what you recorded; your report above stays visible.")
    return {"decision": "block", "reason": (
        f"Before you finish: {len(todo)} commit(s) reached {_branch()} in this session and no fact review "
        f"covers them:\n{shown}{more}\n"
        "If any of it changes a standing truth (a host, a key, a model, a branch or deploy rule, a tool, a "
        "decision someone will look up later), record what is true now, one topic per fact; it retires the "
        "old statement for that topic:\n"
        '  python3 .ai-teammates/hooks/memlife.py fact set "<topic>" "<what is true now>"\n'
        "Current facts: python3 .ai-teammates/hooks/memlife.py fact ls. If nothing standing changed:\n"
        '  python3 .ai-teammates/hooks/memlife.py fact reviewed --why "<one line>"\n' + tail)}


def pending_line() -> str:
    """One pre-flight line: '' when review is off, ok when nothing is unreviewed, else a note."""
    if not _review_on(conf()):
        return ""
    review_init()
    m = _jread(_marker())
    todo = unreviewed([m.get("through", "")])
    since = (m.get("at") or "")[:10]
    if not todo:
        return f"  ok    memory: standing facts reviewed through {m.get('through', '')[:7]} ({since})"
    names = ", ".join(f"{sha} {subject[:40]}" for sha, subject in todo[:3])
    return (f"  note  memory: {len(todo)} commit(s) on {_branch()} since the last fact review ({since}): "
            f"{names}{' ...' if len(todo) > 3 else ''} - check them against the standing facts "
            "(memlife.py fact ls), then fact set or fact reviewed")


def _hook_payload() -> dict:
    """The hook's JSON on stdin, without ever blocking on a terminal or an idle pipe."""
    import select
    try:
        if sys.stdin is None or sys.stdin.isatty() or not select.select([sys.stdin], [], [], 0.5)[0]:
            return {}
        return json.loads(sys.stdin.read() or "{}")
    except Exception:
        return {}


# --- cli ------------------------------------------------------------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="memlife.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fact")
    fs = f.add_subparsers(dest="sub", required=True)
    p = fs.add_parser("set"); p.add_argument("topic"); p.add_argument("statement")
    p = fs.add_parser("retire"); p.add_argument("topic"); p.add_argument("--why", default="")
    p = fs.add_parser("ls"); p.add_argument("--all", action="store_true")
    p = fs.add_parser("reviewed"); p.add_argument("--why", required=True)
    sub.add_parser("pending")
    sub.add_parser("stop-hook")
    p = sub.add_parser("retire-term"); p.add_argument("term"); p.add_argument("--why", default="")
    p = sub.add_parser("supersede"); p.add_argument("id"); p.add_argument("--by", default=""); p.add_argument("--why", default="")
    sub.add_parser("restore").add_argument("id")
    sub.add_parser("file").add_argument("card")
    sub.add_parser("audit")
    for name, sp in list(sub.choices.items()) + list(fs.choices.items()):
        if name not in ("audit", "ls", "fact", "reviewed", "pending", "stop-hook"):
            sp.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    dry = getattr(a, "dry_run", False)
    if a.cmd == "stop-hook":                    # a hook must never break the session it runs in
        try:
            out = stop_hook(_hook_payload())
        except Exception:
            out = {}
        if out:
            print(json.dumps(out))
        return 0
    if a.cmd == "pending":
        try:
            line = pending_line()
        except Exception as e:
            line = f"  note  memory: fact review could not be checked ({type(e).__name__})"
        if line:
            print(line)
        return 0
    try:
        if a.cmd == "fact" and a.sub == "reviewed":
            tip = review_mark(f"reviewed: {a.why.strip()}")
            print(f"  facts reviewed through {tip[:7] or '(no integration branch)'}: {a.why.strip()}")
            return 0
        if a.cmd == "fact" and a.sub == "set":
            print(fact_set(a.topic, a.statement, dry))
        elif a.cmd == "fact" and a.sub == "retire":
            print(fact_retire(a.topic, a.why, dry))
        elif a.cmd == "fact":
            rows = current_facts()
            print(f"  {len(rows)} current fact(s)")
            for r in rows:
                print(f"   {r['topic']} (since {r['since']}): {r['statement']}")
            if a.all:
                for r in retired_drawers():
                    if SUPERSEDED_PREFIX.match(r["doc"]) and r["doc"].split("---\n", 1)[-1].startswith("FACT | "):
                        t, s, _ = parse_fact(r["doc"].split("---\n", 1)[-1])
                        print(f"   (retired {r['when']}) {t}: {s}")
        elif a.cmd == "retire-term":
            print(retire_term(a.term, a.why, dry))
        elif a.cmd == "supersede":
            print(supersede(a.id, a.by, a.why, dry))
        elif a.cmd == "restore":
            print(restore(a.id, dry))
        elif a.cmd == "file":
            print(file_card(a.card, dry))
        else:
            print(audit())
        return 0
    except memslice.MemError as e:
        print(f"  memlife: {e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"  memlife: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
