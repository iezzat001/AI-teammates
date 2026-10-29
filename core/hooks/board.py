#!/usr/bin/env python3
"""Board CLI — move cards as work happens.

A board nobody updates becomes AGENTS_RULES.md: confidently wrong. This
exists so updating is one short command, not a JSON edit.

  board.py move ACC-2 done "reviewed, approved"
  board.py add  SCOUT-2 cto todo "Retrieval endpoint" "idea -> frameworks"
  board.py ls
  board.py ls doing

Statuses are whatever the board's own `columns` say. FOUND 2026-09-19: this
hardcoded backlog/in_progress and expected {id,label} columns, while install.sh
writes plain ids todo/doing/review/done - so `ls` crashed on every fresh install.
"""
import json
import sys
from datetime import date
from pathlib import Path

BOARD = Path(__file__).resolve().parents[1] / "board.json"


def load():
    return json.loads(BOARD.read_text(encoding="utf-8"))


def save(d):
    d["updated"] = date.today().isoformat()
    BOARD.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n",
                     encoding="utf-8")


def columns(d):
    """Accept plain ids (what install.sh writes) and {id, label} objects."""
    return [c if isinstance(c, dict) else {"id": c, "label": c}
            for c in d.get("columns", [])]


def valid(d, status):
    ids = [c["id"] for c in columns(d)]
    if status not in ids:
        print("status must be one of: %s" % ", ".join(ids))
        return False
    return True


# Column names differ per project (a fresh install: todo/doing/review/done;
# ContentOS: backlog/in_progress/review/done). The runner speaks in states and
# this maps them onto whatever columns the board actually has.
STATES = {"doing": ["doing", "in_progress"], "review": ["review"],
          "blocked": ["blocked"], "todo": ["todo", "backlog"]}


def set_state(tid, state, note=None):
    """Move card `tid` to the board's own column for `state` and, if given, record
    the facts in its note. A state the board has no column for leaves the status
    alone (the note still says what happened). Returns the card's status, or ''
    when the card is not on the board."""
    if not BOARD.exists():
        return ""
    d = load()
    ids = [c["id"] for c in columns(d)]
    for t in d["tasks"]:
        if t["id"].lower() != tid.lower():
            continue
        for want in STATES.get(state, [state]):
            if want in ids:
                t["status"] = want
                break
        t["updated"] = date.today().isoformat()
        if note:
            old = t.get("note", "")
            # keep the card's own context once; replace the previous run line
            kept = old.split(" || earlier: ", 1)[1] if old.startswith("run ") and " || earlier: " in old \
                else ("" if old.startswith("run ") else old)
            t["note"] = (note + (" || earlier: " + kept if kept else ""))[:700]
        save(d)
        return t["status"]
    return ""


def ensure(tid, agent, title, note=""):
    """Put the card on the board if it is not there yet (first free column of todo/backlog)."""
    if not BOARD.exists():
        return False
    d = load()
    if any(t["id"].lower() == tid.lower() for t in d["tasks"]):
        return False
    ids = [c["id"] for c in columns(d)]
    status = next((s for s in STATES["todo"] if s in ids), ids[0] if ids else "todo")
    d["tasks"].append({"id": tid, "title": title, "agent": agent, "status": status,
                       "updated": date.today().isoformat(), "note": note})
    save(d)
    return True


def cmd_ls(args):
    """`ls` every column; `ls <column>` one; `ls --open` everything not done. The CTO's brief
    carries `ls --open` instead of the whole board.json: on ContentOS the board is 100 cards, 77
    of them done, about 24k tokens re-sent with every call of a session (measured 2026-10-05)."""
    d = load()
    open_only = "--open" in args
    args = [a for a in args if a != "--open"]
    want = args[0] if args else None
    for col in columns(d):
        if want and col["id"] != want:
            continue
        if open_only and col["id"] == "done":
            continue
        items = [t for t in d["tasks"] if t["status"] == col["id"]]
        print("\n%s (%d)" % (col["label"].upper(), len(items)))
        for t in items:
            print("  %-9s %-46s %s" % (t["id"], t["title"][:46], t["agent"]))
    if open_only:
        done = sum(1 for t in d["tasks"] if t["status"] == "done")
        print("\n(%d done cards not listed. One card in full: board.py show <ID>)" % done)


def cmd_show(args):
    """One card in full: status, agent, dates and the whole note."""
    if not args:
        return print("usage: board.py show <ID>")
    for t in load()["tasks"]:
        if t["id"].lower() == args[0].lower():
            print("%s  [%s]  %s  (agent %s, updated %s)\n\n%s"
                  % (t["id"], t["status"], t["title"], t.get("agent"), t.get("updated"), t.get("note") or "(no note)"))
            return
    print("no task %s" % args[0])


def cmd_move(args):
    if len(args) < 2:
        return print("usage: board.py move <ID> <status> [note]")
    tid, status = args[0], args[1]
    d = load()
    if not valid(d, status):
        return
    for t in d["tasks"]:
        if t["id"].lower() == tid.lower():
            t["status"] = status
            t["updated"] = date.today().isoformat()
            if len(args) > 2:
                t["note"] = " ".join(args[2:])
            save(d)
            return print("%s -> %s" % (t["id"], status))
    print("no task %s" % tid)


def cmd_add(args):
    if len(args) < 4:
        return print('usage: board.py add <ID> <agent> <status> "<title>" ["<note>"]')
    tid, agent, status, title = args[0], args[1], args[2], args[3]
    d = load()
    if not valid(d, status):
        return
    if any(t["id"].lower() == tid.lower() for t in d["tasks"]):
        return print("%s already exists" % tid)
    d["tasks"].append({
        "id": tid, "title": title, "agent": agent, "status": status,
        "updated": date.today().isoformat(),
        "note": args[4] if len(args) > 4 else "",
    })
    save(d)
    print("added %s" % tid)


def main():
    if not BOARD.exists():
        return print("board.json missing")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "ls"
    args = sys.argv[2:]
    {"ls": cmd_ls, "show": cmd_show, "move": cmd_move, "add": cmd_add}.get(
        cmd, lambda a: print(__doc__))(args)


if __name__ == "__main__":
    main()
