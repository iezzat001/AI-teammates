"""Project-owned registrations for Codex CLI and migration of framework-owned settings."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from codex_runtime import ROUTES

PREFIX = 'AI_TEAMMATES_RUNTIME=codex PYTHONDONTWRITEBYTECODE=1 python3 "$(git rev-parse --show-toplevel)/.ai-teammates/hooks/'
REGISTRATION = {event: PREFIX + 'codex_hooks.py" ' + event for event in
                ("PreToolUse", "PostToolUse", "PermissionRequest", "UserPromptSubmit", "SessionStart", "SessionEnd", "Stop")}
SCRIPTS = {"PreToolUse": [("guard.py",)], "PostToolUse": [("activity.py",), ("loopguard.py", "PostToolUse")],
           "PermissionRequest": [("loopguard.py", "PermissionRequest")],
           "UserPromptSubmit": [("activity.py",), ("loopguard.py", "UserPromptSubmit")],
           "SessionStart": [("session_start.py",)], "SessionEnd": [("session_end.py",)], "Stop": [("memlife.py", "stop-hook")]}


def save_json(path, data):
    old = path.read_text() if path.exists() else None
    new = json.dumps(data, indent=2, ensure_ascii=False) + ("\n" if old is None or old.endswith("\n") else "")
    if new != old:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new)


def registration(root, install):
    for path in (root / ".codex/hooks.json", root / ".claude/settings.json"):
        codex = path.parent.name == ".codex"
        if not path.exists() and not (install and codex):
            continue
        data = json.loads(path.read_text()) if path.exists() else {}
        original = json.dumps(data)
        hooks = data.setdefault("hooks", {})
        for event, groups in list(hooks.items()):
            for group in groups:
                def ours(handler):
                    cmd = handler.get("command", "")
                    if codex:
                        return cmd in REGISTRATION.values()
                    return any(cmd == 'python3 "$CLAUDE_PROJECT_DIR"/.ai-teammates/hooks/' + " ".join(parts)
                               for parts in SCRIPTS.get(event, []) + ([("loopguard.py", event)] if event == "PostToolUseFailure" else []))
                group["hooks"] = [h for h in group.get("hooks", []) if not ours(h)]
            hooks[event] = [g for g in groups if g["hooks"]]
            if not hooks[event]:
                del hooks[event]
        if install and codex:
            for event, cmd in REGISTRATION.items():
                hooks.setdefault(event, []).append({"hooks": [{"type": "command", "command": cmd}]})
        if not hooks and '"hooks"' not in original:
            data.pop("hooks")
        if json.dumps(data) != original:
            save_json(path, data)


def migrate(root):
    ai = root / ".ai-teammates"
    path = ai / "config.json"
    cfg = json.loads(path.read_text())
    orig = json.dumps(cfg)
    cto = cfg.setdefault("cto", {})
    legacy = cto.get("model", "default") == "default" or str(cto.get("model")).startswith("claude-")
    if legacy:
        cto.update(model=ROUTES["cto"][0], effort=ROUTES["cto"][1])
    if cto.get("provider") == "anthropic":
        cto.pop("provider")
    for section, keys in ((cto, ("max_usd",)), (cfg.get("cards", {}), ("default_usd", "max_usd")),
                          (cfg.get("budget", {}), ("daily_usd",))):
        for key in keys:
            section.pop(key, None)
    tiers = cfg.get("seat_models", {})
    for k in list(tiers):
        if str(tiers[k]).startswith("claude-"):
            del tiers[k]
    if orig != json.dumps(cfg):
        save_json(path, cfg)
    for path in (ai / "agents").rglob("*.md"):
        text = path.read_text()
        m = re.match(r"^(---\n)(.*?)(\n---\n)", text, re.S)
        if not m or not re.search(r"^harness: claude-code\s*$", m[2], re.M):
            continue
        head = re.sub(r"^harness: claude-code$", "harness: codex", m[2], flags=re.M)
        head = re.sub(r"^model: claude-.*$", "model: " + ROUTES["standard"][0], head, flags=re.M)
        head = re.sub(r"^(?:launch|headless): claude[^\n]*\n?", "", head, flags=re.M)
        path.write_text(m[1] + head.rstrip("\n") + m[3] + text[m.end():])


def dispatch(event):
    payload = sys.stdin.buffer.read()
    try:
        data = json.loads(payload)
    except ValueError:
        return 0
    if not isinstance(data, dict):
        return 0
    for script, *args in SCRIPTS[event]:
        if (script == "loopguard.py" and event == "PostToolUse" and data.get("tool_name") == "Bash"
                and os.environ.get("AI_TEAMMATES_SUPERVISED") == "1"):
            continue  # The supervisor forwards the shell result with its exit code.
        p = subprocess.run([sys.executable, str(Path(__file__).with_name(script)), *args],
                           input=payload, capture_output=True)
        if p.returncode and event == "PreToolUse":
            sys.stderr.buffer.write(p.stderr or b"Could not check the pending tool call.")
            return 2
        if event == "SessionStart":
            print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": p.stdout.decode(errors="replace")}}))
        elif event == "PermissionRequest" and p.stdout.strip():
            decision = json.loads(p.stdout)
            decision["hookSpecificOutput"]["decision"].pop("interrupt", None)
            print(json.dumps(decision))
        elif event != "SessionEnd" and p.stdout.strip():
            sys.stdout.buffer.write(p.stdout)
    return 0


if __name__ == "__main__":
    action = sys.argv[1]
    if action in ("install", "uninstall"):
        root = Path(sys.argv[2])
        registration(root, action == "install")
        if action == "install":
            migrate(root)
    else:
        sys.exit(dispatch(action))
