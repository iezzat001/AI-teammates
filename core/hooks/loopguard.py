#!/usr/bin/env python3
"""loopguard - stop an agent that keeps retrying what only the human can fix.

THE PROBLEM (measured on the pilot project, 2026-10-02). In 26 CTO sessions, 101 of 230
tool errors were "requires approval" denials, all in headless `cto "task"` runs.
After a denial the model tried a variant verb 42 times, another tool 27 times,
and said nothing first in 73 of 95 continuations. The worst chain was nine
failures in a row to make one symlink: a thing the owner does in two seconds.
After a guard.py block it asked the human 1 time in 10, and once it edited
guard.py itself instead. The old event log could not even see it: PostToolUse
only fires on SUCCESS, so all 202 recorded events were ok=true.

WHAT WORKS (verified by a spike against claude 2.1.285, same date):
  * a command that fails        -> PostToolUseFailure fires (payload has `error`)
  * an approval denial, headless-> PreToolUse then PermissionRequest fire;
                                   PostToolUseFailure and PermissionDenied do NOT
  * `continue:false` from a failure hook does NOT stop the run
  * a PermissionRequest hook can deny with a message the model obeys, and with
    interrupt:true it ends the run on the spot
  * PreToolUse `ask` outranks an explicit allow rule; headless it auto-denies

SO THE DESIGN IS
  count failures by CLASS, not by command (the model rotates variants)
  -> trip when the human must act: 2 human-only failures in a row, or 4 of any
     kind (a failing test or a bare `exit 1` is the work, never a loop)
  -> on trip write a BLOCKED record and set a gate: guard.py then answers `ask`
     to every tool that is not read-only, until the human speaks
  -> headless there is nobody to ask, so a PermissionRequest denial carries the
     message "BLOCKED: <class> | <need>"; the next one interrupts the run, and
     the launcher prints what the human has to do.

    loopguard.py <Event>        hook entry (stdin: the hook payload)
    loopguard.py report [--since ISO]   print unresolved BLOCKED records
    loopguard.py resolve                mark them all resolved (the human dealt with them)
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _config  # noqa: E402

DEFAULTS = {
    "human_threshold": 2,       # consecutive human-only failures before the guard trips
    "any_threshold": 4,         # consecutive failures of any other kind
    "interrupt_after": 2,       # headless: denials before the run is ended
    "seat_interrupt_after": 2,  # a card seat gets one refusal it can finish after (see card_denied_text)
    "streak_max": 8,
}

# (name, kind, regex). kind: human = only the owner can fix it; wait = transient.
# Order matters: first match wins. Lower-cased error text is searched.
CLASSES = [
    ("approval", "human", r"requires approval|haven'?t granted|hasn'?t been granted|has not been granted|"
                          r"multiple operations|simple_expansion|contains [\w ]{0,20}expansion|"
                          r"may only change directories|requires permission|not allowed to use|"
                          r"permission to use [^\n]{0,40}was denied|no approval surface"),
    ("auth", "human", r"\b(?:http|status|error|code)[ :=]*40[123]\b|\b40[123] (?:unauthorized|forbidden|payment)|"
                      r"unauthori[sz]ed|not (?:logged|signed) in|token (?:has )?expired|oauth[^\n]{0,30}expired|"
                      r"credit balance|usage credits|insufficient (?:credit|quota|funds)|invalid api[ _-]?key|"
                      r"authentication (?:failed|required)|login required"),
    ("interactive", "human", r"sudo:|a password is required|password is required|\[y/n\]|\(yes/no|passphrase|"
                             r"permission denied \(publickey|host key verification failed|"
                             r"are you sure you want to continue"),
    ("missing-tool", "human", r"command not found|not installed|is not recognized as an internal"),
    ("os-permission", "human", r"permission denied|operation not permitted|\beacces\b|\beperm\b"),
    ("rate-limit", "wait", r"\b429\b|rate.?limit|too many requests|quota exceeded|overloaded"),
    ("network", "wait", r"econnrefused|econnreset|etimedout|could not resolve|network is unreachable|"
                        r"connection (?:refused|timed out)"),
]
# A failing test, lint or build IS the work. Counting it as a loop would fire
# during every ordinary red-green cycle.
TEST_CMD = re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:test|lint|typecheck|check|build)\b|"
                      r"\b(?:vitest|jest|pytest|tsc|eslint|ruff|mypy|cargo\s+(?:test|check|clippy)|"
                      r"go\s+(?:test|vet)|make\s+(?:test|check))\b")
# Tools that cannot change anything. Everything else is gated once tripped.
READ_ONLY = {"Read", "Grep", "Glob", "LS", "TodoWrite", "NotebookRead", "WebSearch"}

MARK = "<redacted>"


def cfg() -> dict:
    out = dict(DEFAULTS)
    out.update(_config.load().get("loopguard") or {})
    return out


def classes() -> list:
    extra = [(c["name"], c.get("kind", "human"), c["pattern"])
             for c in (_config.load().get("loopguard") or {}).get("extra_classes", [])
             if c.get("name") and c.get("pattern")]
    return extra + CLASSES


def scrub(text) -> str:
    try:
        from activity import scrub as s
        return s(str(text or ""))
    except Exception:
        return str(text or "")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_headless() -> bool:
    """Nobody to ask. `cto "task"` exports the first; a card seat sets the second."""
    return os.environ.get("AI_TEAMMATES_HEADLESS") == "1" or bool(os.environ.get("AI_TEAMMATES_CARD"))


def interrupt_after() -> int:
    c = cfg()
    return int(c["seat_interrupt_after"] if os.environ.get("AI_TEAMMATES_CARD") else c["interrupt_after"])


# --- state -----------------------------------------------------------------------

def _dir(name: str) -> Path:
    d = _config.state_dir() / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sfile(sid: str) -> Path:
    return _dir("loopguard") / (re.sub(r"[^A-Za-z0-9_-]", "_", sid or "nosession")[:64] + ".json")


def load_state(sid: str) -> dict:
    try:
        st = json.loads(_sfile(sid).read_text())
        if isinstance(st, dict):
            return st
    except Exception:
        pass
    return {"streak": [], "blocked": None, "denials": 0}


def save_state(sid: str, st: dict) -> None:
    f = _sfile(sid)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False))
    os.replace(tmp, f)
    # a day-old state file belongs to a session that is gone
    for old in f.parent.glob("*.json"):
        try:
            if time.time() - old.stat().st_mtime > 172800:
                old.unlink()
        except OSError:
            pass


# --- classification ----------------------------------------------------------------

def describe(tool: str, ti: dict) -> str:
    ti = ti or {}
    if tool == "Bash":
        return scrub(" ".join((ti.get("command") or "").split()))[:140]
    for k in ("file_path", "path", "notebook_path", "url", "pattern"):
        if ti.get(k):
            return scrub(f"{tool} {ti[k]}")[:140]
    return tool


def classify(tool: str, ti: dict, error: str):
    """-> (name, kind). kind: human | wait | other | test | benign (last two never count)."""
    err = (error or "").strip()
    if not err or re.fullmatch(r"exit code 1", err, re.I):
        return "benign", "benign"                 # grep/diff/test said no; that is an answer
    if tool == "Bash" and TEST_CMD.search((ti or {}).get("command") or ""):
        return "test", "test"
    low = err.lower()
    for name, kind, rx in classes():
        if re.search(rx, low):
            return name, kind
    return "other", "other"


def suggest(tool: str, ti: dict) -> str:
    """The exact allow-list entry that would unblock a denied Bash call, or ''."""
    if tool != "Bash":
        return ""
    cmd = " ".join(((ti or {}).get("command") or "").split())
    if not cmd or re.search(r"&&|\|\||;|\||\$\(|`|\$\{?\w|>|<", cmd):
        return ""                                  # compound: allowing a prefix would not help
    return " ".join(cmd.split()[:2])


NEED = {
    "approval": "approve this action, or widen what this agent may run",
    "auth": "re-authenticate or fix credentials/billing for the service that answered",
    "interactive": "do the interactive step yourself (sudo, password, passphrase, y/n)",
    "missing-tool": "install the missing tool",
    "os-permission": "fix the file or system permission",
    "rate-limit": "wait, or check the service's limits",
    "network": "check the network or that the service is up",
    "other": "decide how to proceed; the same kind of failure keeps repeating",
}


def need_for(name: str) -> str:
    if name.startswith("guard:"):
        return f"decide whether guardrail '{name[6:]}' should be overridden (only you can: AI_TEAMMATES_OVERRIDE)"
    return NEED.get(name, NEED["other"])


def ask_text(rec: dict) -> str:
    tried = "; ".join(rec.get("tried") or []) or "(nothing recorded)"
    lines = [
        f"STOP. You are blocked on something only the human can resolve ({rec['class']}). "
        "Do not try another spelling, a workaround, a different tool, or an edit to any guardrail.",
        "Reply now with exactly this, then end your turn:",
        f"  BLOCKED: {rec['class']} | {rec['need']}",
        f"  I tried: {tried}",
        f"  You need to: {rec['need']}" + (f"  (allow rule: {rec['suggest']})" if rec.get("suggest") else ""),
    ]
    return "\n".join(lines)


def card_denied_text(desc: str) -> str:
    """First refusal inside a card run. FOUND 2026-10-02 on the DOC-2 pilot: a seat finished
    its work perfectly (3 correct edits, acceptance green) and then tried to self-check with a
    command the card had not allowed. Ending the run on that one denial threw away a correct
    result, so the first refusal tells it how to finish; a SECOND ends the run."""
    return ("That command is not allowed for this card: " + desc[:100] + ". Do not retry it, rephrase it, "
            "or try another tool for it. If the card cannot be finished without it, end your turn with exactly: "
            "BLOCKED: approval | <what you need>. If it was only a way to double-check your work, skip it and "
            "finish; to check yourself run `python3 .ai-teammates/hooks/card.py accept`.")


# --- recording -------------------------------------------------------------------

def _event(kind: str, detail: dict) -> None:
    try:
        rec = {"ts": now(), "kind": kind, "agent": os.environ.get("CLAUDE_AGENT_NAME", "main")}
        rec.update(detail)
        with (_config.state_dir() / "events.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def make_record(sid: str, st: dict, name: str, kind: str, evidence: str, sug: str = "",
                tried=None, need=None, card=None, who=None) -> dict:
    """need/card/who are for a record made OUTSIDE a hook (cardrun persisting a seat's
    own `BLOCKED:` line); inside a hook they come from the class and the environment."""
    tried = tried if tried is not None else [e.get("cmd", "") for e in st.get("streak", [])][-3:]
    env_card = os.environ.get("AI_TEAMMATES_CARD")
    rec = {
        "ts": now(), "session": sid, "who": who or os.environ.get("CLAUDE_AGENT_NAME", "main"),
        "card": card or (Path(env_card).stem if env_card else None), "class": name, "kind": kind,
        "need": need or need_for(name), "evidence": scrub(evidence)[:300], "tried": [t for t in tried if t],
        "suggest": sug, "resolved": None,
    }
    f = _dir("blocked") / f"{now().replace(':', '-')}-{re.sub(r'[^A-Za-z0-9]', '', sid)[:8] or 'nosess'}.json"
    f.write_text(json.dumps(rec, ensure_ascii=False, indent=1))
    rec["_file"] = f.name
    _event("blocked", {"rule": f"loopguard:{name}", "message": rec["need"]})
    return rec


def _trip(st: dict):
    """Which streak condition fires, as (class, kind), or None."""
    c, s = cfg(), st.get("streak", [])
    h = int(c["human_threshold"])
    if len(s) >= h and all(e["kind"] == "human" for e in s[-h:]):
        return s[-1]["class"], "human"
    if len(s) >= int(c["any_threshold"]):
        return s[-1]["class"], s[-1]["kind"]
    return None


def _push(st: dict, tool: str, name: str, kind: str, cmd: str, err: str) -> None:
    st.setdefault("streak", []).append({"tool": tool, "class": name, "kind": kind,
                                       "cmd": cmd, "err": scrub(err)[:120]})
    st["streak"] = st["streak"][-int(cfg()["streak_max"]):]


# --- hook handlers -----------------------------------------------------------------

def on_failure(p: dict) -> dict:
    sid, tool = p.get("session_id", ""), p.get("tool_name", "")
    ti = p.get("tool_input") or {}
    if not sid or p.get("is_interrupt"):
        return {}                                  # the human pressed Esc: not a failure
    name, kind = classify(tool, ti, str(p.get("error") or ""))
    _event("tool", {"tool": tool, "target": describe(tool, ti)[:80], "ok": False,
                    "class": name, "error": scrub(str(p.get("error") or ""))[:120]})
    if kind in ("benign", "test"):
        return {}
    st = load_state(sid)
    _push(st, tool, name, kind, describe(tool, ti), str(p.get("error") or ""))
    out = {}
    trip = _trip(st)
    if trip and not st.get("blocked"):
        st["blocked"] = make_record(sid, st, trip[0], trip[1], str(p.get("error") or ""),
                                    suggest(tool, ti))
        out = {"hookSpecificOutput": {"hookEventName": "PostToolUseFailure",
                                      "additionalContext": ask_text(st["blocked"])}}
    save_state(sid, st)
    return out


def on_success(p: dict) -> dict:
    sid, tool = p.get("session_id", ""), p.get("tool_name", "")
    if not sid:
        return {}
    st = load_state(sid)
    if any(e["tool"] == tool for e in st.get("streak", [])):
        st["streak"] = [e for e in st["streak"] if e["tool"] != tool]
        save_state(sid, st)
    return {}


def on_permission_request(p: dict) -> dict:
    """Interactive: the human sees the dialog, so this stays silent. Headless there
    is nobody to answer: deny with the ask, and end the run on the next attempt."""
    sid, tool = p.get("session_id", ""), p.get("tool_name", "")
    if not is_headless() or not sid:
        return {}
    ti = p.get("tool_input") or {}
    st = load_state(sid)
    st["denials"] = int(st.get("denials", 0)) + 1
    if not st.get("blocked"):
        st["blocked"] = make_record(sid, st, "approval", "human", f"approval needed for: {describe(tool, ti)}",
                                    suggest(tool, ti), tried=[describe(tool, ti)])
        st["from_denial"] = True            # created here, not by a failure streak
    rec = st["blocked"]
    save_state(sid, st)
    limit = interrupt_after()
    gentle = (bool(os.environ.get("AI_TEAMMATES_CARD")) and st.get("from_denial")
              and st["denials"] < limit)
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {
        "behavior": "deny", "message": card_denied_text(describe(tool, ti)) if gentle else ask_text(rec),
        "interrupt": st["denials"] >= limit}}}


def on_prompt(p: dict) -> dict:
    """The human spoke: whatever blocked is theirs to have resolved, or to keep blocking."""
    sid = p.get("session_id", "")
    if not sid:
        return {}
    st = load_state(sid)
    rec = st.get("blocked")
    if rec and rec.get("_file"):
        f = _dir("blocked") / rec["_file"]
        try:
            d = json.loads(f.read_text())
            d["resolved"] = now()
            f.write_text(json.dumps(d, ensure_ascii=False, indent=1))
        except Exception:
            pass
    if st.get("streak") or st.get("blocked") or st.get("denials"):
        save_state(sid, {"streak": [], "blocked": None, "denials": 0})
    return {}


# --- called from guard.py ----------------------------------------------------------

def note_guard_block(sid: str, rule: str, cmd: str) -> None:
    """guard.py blocks BEFORE the tool runs, so no failure event follows. Without
    this a block loop (retry, rephrase, retry) was invisible to the counter."""
    if not sid:
        return                                     # nothing to attribute it to
    st = load_state(sid)
    _push(st, "guard", f"guard:{rule}", "human", scrub(cmd)[:140], f"guard {rule}")
    trip = _trip(st)
    if trip and not st.get("blocked"):
        st["blocked"] = make_record(sid, st, trip[0], trip[1], f"guard rule '{rule}' fired repeatedly")
    save_state(sid, st)


def gate(sid: str, tool: str):
    """A reason string while the session is blocked and `tool` could change something."""
    if tool in READ_ONLY or not sid:
        return None
    rec = load_state(sid).get("blocked")
    return ask_text(rec) if rec else None


# --- report ------------------------------------------------------------------------

def unresolved(since: str = "") -> list:
    out = []
    for f in sorted(_dir("blocked").glob("*.json")):
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        if d.get("resolved") is None and (not since or d.get("ts", "") >= since):
            out.append(d)
    return out


def report(since: str = "") -> int:
    items = unresolved(since)
    if not items:
        return 0
    print(f"\n  NEEDS YOU — {len(items)} blocked item(s) only you can resolve:")
    for d in items[-6:]:
        who = d["who"] + (f" on card {d['card']}" if d.get("card") else "")
        print(f"    [{d['class']}] {who} at {d['ts'][11:16]}Z")
        print(f"      you need to: {d['need']}")
        if d.get("evidence"):
            print(f"      evidence:    {d['evidence'][:150]}")
        if d.get("tried"):
            print(f"      tried:       {'; '.join(d['tried'])[:150]}")
        if d.get("suggest"):
            print(f"      allow rule:  add \"{d['suggest']}\" to .ai-teammates/config.json cto.allow")
    return 1


def resolve_card(cid: str) -> int:
    """Re-running a card that was blocked means the human dealt with what blocked it."""
    n = 0
    for f in _dir("blocked").glob("*.json"):
        try:
            d = json.loads(f.read_text())
            if d.get("resolved") is None and d.get("card") == cid:
                d["resolved"] = now()
                f.write_text(json.dumps(d, ensure_ascii=False, indent=1))
                n += 1
        except Exception:
            continue
    return n


def resolve_all() -> int:
    """The human dealt with whatever earlier runs were blocked on."""
    n = 0
    for f in _dir("blocked").glob("*.json"):
        try:
            d = json.loads(f.read_text())
            if d.get("resolved") is None:
                d["resolved"] = now()
                f.write_text(json.dumps(d, ensure_ascii=False, indent=1))
                n += 1
        except Exception:
            continue
    print(f"  resolved {n} blocked item(s)")
    return 0


HANDLERS = {"PostToolUseFailure": on_failure, "PostToolUse": on_success,
            "PermissionRequest": on_permission_request, "UserPromptSubmit": on_prompt}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        since = sys.argv[sys.argv.index("--since") + 1] if "--since" in sys.argv else ""
        return report(since)
    if len(sys.argv) > 1 and sys.argv[1] == "resolve":
        return resolve_all()
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0                                   # never break a session on a bad payload
    event = sys.argv[1] if len(sys.argv) > 1 else payload.get("hook_event_name", "")
    try:
        out = HANDLERS.get(event, lambda p: {})(payload)
        if out:
            print(json.dumps(out))
    except Exception as e:                         # a guard that crashes must not take the session with it
        _event("warn", {"rule": "loopguard-crashed", "message": f"{type(e).__name__}: {e}"[:200]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
