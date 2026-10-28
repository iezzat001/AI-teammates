#!/usr/bin/env python3
"""Is a seat running RIGHT NOW? Check processes, not the completed-runs log.

    python3 .ai-teammates/hooks/seat_status.py

Why this exists: on 2026-09-06 the Security Audit seat was running and I said
it had failed. I checked agent_runs.jsonl - which seat.py writes only when a
process EXITS - saw the previous seat's entry, and concluded the run never
started. The human corrected me from the Kimi dashboard, which showed live requests.

A running seat and a seat that never started look identical in the log. The
only honest source for "is it running" is the process table.

Nearly told him to re-run, which would have launched a second concurrent audit.
"""
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _config  # noqa: E402
RUNS = _config.state_dir() / "agent_runs.jsonl"


def _ppid_map(rows):
    return {pid: (ppid, cmd) for pid, ppid, cmd in rows}


def _has_seat_ancestor(pid, procs, depth=0):
    """Walk the parent chain. A seat's harness is a Popen child of
    `seat.py run <name>` (Popen+wait), so seat.py sits in its ancestry.
    The CTO session is exec'd straight from ./cto - no seat.py parent."""
    if depth > 10:
        return False
    entry = procs.get(str(pid))
    if not entry:
        return False
    ppid, _ = entry
    if ppid == "0" or ppid == "1" or ppid == str(pid):
        return False
    parent = procs.get(ppid)
    if not parent:
        return False
    if "seat.py" in parent[1]:
        return True
    return _has_seat_ancestor(ppid, procs, depth + 1)


def running_seats():
    """Live seat processes, from ps.

    `ollama launch (claude|opencode|codex)` matches BOTH seats and the CTO's
    own session - on 2026-09-06 the CTO pre-flight flagged the CTO session
    itself as a running seat. Distinguish by ancestry: a seat is a child of
    `seat.py run`. Anything else is reported as non-seat, informational."""
    try:
        out = subprocess.run(["ps", "-eo", "pid,ppid,command"],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return [], []
    rows = []
    for line in out.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        rows.append((parts[0], parts[1], parts[2]))
    procs = _ppid_map(rows)
    seats, others = [], []
    for pid, ppid, cmd in rows:
        if "grep" in cmd:
            continue
        hit = re.search(r"seat\.py run|ollama launch (claude|opencode|codex)", cmd)
        if not hit:
            continue
        if "ollama launch" in cmd and not _has_seat_ancestor(pid, procs):
            others.append({"pid": pid, "cmd": cmd[:70]})
            continue
        m = re.search(r"--model\s+'?([\w.:\-\[\]]+)'?", cmd)
        # `seat.py run <seat> ...`  or  `seat.py run --card <ID>` (the seat is on the card).
        # Reading "--card" as the seat name made card runs invisible to the
        # double-run check: a check pointed at a name that was not there.
        s = re.search(r"seat\.py run\s+(?:--card\s+(\S+)|(\S+))", cmd)
        who = (s.group(2) or _card_seat(s.group(1))) if s else None
        seats.append({"pid": pid, "etime": _etime(pid, procs),
                      "model": m.group(1) if m else "?",
                      "seat": who,
                      "cmd": cmd[:70]})
    return seats, others


def _card_seat(cid):
    """Which seat a card belongs to. Unknown (another project's card): 'card:<ID>'."""
    try:
        return json.loads((REPO / ".ai-teammates" / "cards" / f"{cid}.json").read_text())["seat"]
    except Exception:
        return f"card:{cid}"


def _etime(pid, procs):
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "etime="],
                             capture_output=True, text=True, timeout=10).stdout
        return out.strip() or "?"
    except Exception:
        return "?"


def last_completed():
    try:
        rows = [json.loads(l) for l in RUNS.read_text().splitlines() if l.strip()]
        return rows[-1] if rows else None
    except Exception:
        return None


def main():
    live, others = running_seats()
    if live:
        print(f"  {len(live)} seat process(es) RUNNING:")
        for h in live:
            label = h["seat"] or h["model"]
            print(f"    pid {h['pid']:>7}  {h['etime']:>9}  {label}")
        print("\n  Do NOT launch the same seat again - you would get two "
              "concurrent runs.")
    else:
        print("  no seat running")
    if others:
        print(f"\n  {len(others)} non-seat ollama-launch process(es) "
              "(not children of seat.py - the CTO's own session matches this):")
        for h in others:
            print(f"    pid {h['pid']:>7}  {h['cmd']}")

    r = last_completed()
    if r:
        age = "?"
        try:
            t = datetime.fromisoformat(r["ts"])
            age = f"{(datetime.now(timezone.utc) - t).total_seconds() / 60:.0f}m ago"
        except Exception:
            pass
        print(f"\n  last COMPLETED run: {r.get('seat')} · {r.get('seconds')}s · "
              f"exit {r.get('exit')} · {age}")
        print("  (this log only gets a row when a process exits - a running "
              "seat never appears here)")
    return 0 if not live else 1


if __name__ == "__main__":
    sys.exit(main())
