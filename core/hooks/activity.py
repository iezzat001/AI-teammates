#!/usr/bin/env python3
"""PostToolUse activity logger - the data feed for the team dashboard.

Every tool call appends one line to .ai-teammates/events.jsonl. That file is the
whole visibility layer: which agent, doing what, to which file, when. No
server, no polling a model, no token cost.

Deliberately lossy - it records the shape of the work, not its content, so the
log stays small and readable and never leaks secrets.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _config  # noqa: E402
EVENTS = _config.state_dir() / "events.jsonl"
MAX_LINES = 2000  # keep the file bounded; the dashboard only shows recent work

# Widened 2026-08-23 after a kimi-k3 audit found the original missed two
# formats this repo actually uses: Supabase JWTs (eyJ...) and Google/Gemini
# keys (AIza...). Either would have landed unredacted in events.jsonl.
SECRET = re.compile(
    r"("
    r"sk_[A-Za-z0-9_\-]{8,}"           # honcho, generic
    r"|gsk_[A-Za-z0-9_\-]{8,}"         # groq
    r"|hf_[A-Za-z0-9_\-]{8,}"          # huggingface
    r"|apify_api_[A-Za-z0-9_\-]{8,}"
    r"|monid_(?:live|test)_[A-Za-z0-9_\-]{8,}"
    r"|hch-[A-Za-z0-9_\-]{8,}"         # honcho cloud
    r"|AIza[A-Za-z0-9_\-]{20,}"        # google / gemini
    r"|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]+"  # JWT (supabase)
    r"|xox[baprs]-[A-Za-z0-9\-]{8,}"   # slack
    r"|ghp_[A-Za-z0-9]{20,}"           # github PAT
    r"|Bearer\s+\S+"
    r"|Authorization:\s*\S+"
    r")"
)


def scrub(text):
    return SECRET.sub("<redacted>", text or "")


def summarise(tool, ti):
    """One short human-readable line describing the action."""
    if tool == "Bash":
        cmd = " ".join((ti.get("command") or "").split())
        # first meaningful token, e.g. "git commit", "curl", "systemctl restart"
        m = re.match(r"(?:cd\s+\S+\s*&&\s*)?(\S+(?:\s+\S+)?)", cmd)
        return scrub(m.group(1) if m else cmd)[:80]
    for key in ("file_path", "path", "notebook_path"):
        if ti.get(key):
            return str(ti[key]).replace(str(REPO) + "/", "")[:80]
    if tool in ("Grep", "Glob"):
        return scrub(str(ti.get("pattern") or ""))[:60]
    if tool == "WebFetch":
        return scrub(str(ti.get("url") or ""))[:80]      # keys ride in query strings
    if tool == "Task":
        return scrub(str(ti.get("description") or ""))[:80]
    return ""


def trim():
    """Keep events.jsonl bounded so it never becomes a liability."""
    try:
        if not EVENTS.exists():
            return
        lines = EVENTS.read_text(encoding="utf-8").splitlines()
        if len(lines) > MAX_LINES:
            EVENTS.write_text("\n".join(lines[-MAX_LINES:]) + "\n",
                              encoding="utf-8")
    except Exception:
        pass


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tool = payload.get("tool_name") or payload.get("tool") or ""
    ti = payload.get("tool_input") or {}
    resp = payload.get("tool_response") or {}

    ok = True
    if isinstance(resp, dict):
        ok = not resp.get("is_error", False)

    try:
        EVENTS.parent.mkdir(parents=True, exist_ok=True)
        with EVENTS.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "kind": "tool",
                "agent": os.environ.get("CLAUDE_AGENT_NAME", "main"),
                "tool": tool,
                "target": summarise(tool, ti),
                "ok": ok,
            }, ensure_ascii=False) + "\n")
        trim()
    except Exception:
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
