#!/usr/bin/env bash
# Install AI-teammates into a project.
#
#   cd ~/your-project && /path/to/AI-teammates/install.sh
#
# Copies the hooks, agent specs and docs into <project>/.ai-teammates, writes a
# config, puts `cto` on your PATH, and leaves the project's own files alone.
#
# Re-running upgrades the hooks and docs and preserves board.json and config.

set -euo pipefail

# python3 resolves our own path just below, so it is checked before anything uses it.
command -v python3 >/dev/null || { echo "python3 is required (3.9 or newer)." >&2; exit 1; }

# Resolve symlinks: `ai-teammates` on PATH is a link to this file, and dirname
# of the LINK is ~/.local/bin - so every `ai-teammates install` failed at cp.
SRC="$(dirname "$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "${BASH_SOURCE[0]}")")"
export PATH="/usr/bin:$PATH"

MODE="${1:-install}"
case "$MODE" in
  install|uninstall) ;;
  doctor)  exec "$SRC/bin/cto" --doctor ;;
  *)       echo "usage: ai-teammates [install|doctor|uninstall]" >&2; exit 2 ;;
esac

PROJECT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "Run this from inside a git repository." >&2; exit 1; }
DEST="$PROJECT/.ai-teammates"
NAME="$(basename "$PROJECT")"

if [ "$MODE" = uninstall ]; then
  # `rm -rf .ai-teammates` (the old instructions) left the hooks registered: a
  # missing script makes python exit 2, which Claude Code's PreToolUse reads as
  # BLOCK - every tool call in the project refused. Unregister first.
  python3 - "$PROJECT" <<'PY'
import json, re, sys
from pathlib import Path
sp = Path(sys.argv[1]) / ".claude" / "settings.json"
if sp.exists():
    s = json.loads(sp.read_text())
    for event, entries in list(s.get("hooks", {}).items()):
        for e in entries:
            e["hooks"] = [h for h in e.get("hooks", [])
                          if ".ai-teammates/hooks/" not in h.get("command", "")]
        s["hooks"][event] = [e for e in entries if e["hooks"]]
        if not s["hooks"][event]:
            del s["hooks"][event]
    sp.write_text(json.dumps(s, indent=2))
    print("  claude  : hooks unregistered")
PY
  HOOK="$(cd "$PROJECT" && python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import _config; print(_config.hooks_dir())' "$DEST/hooks")/pre-push"
  grep -q "AI-teammates pre-push" "$HOOK" 2>/dev/null && rm "$HOOK" && echo "  git     : pre-push hook removed"
  rm -rf "$DEST" && echo "  removed : .ai-teammates/ (board, facts and config are in git history)"
  echo "  kept    : opencode.json deny-list, and ~/.local/bin/cto (shared by other projects)"
  exit 0
fi

echo "── installing AI-teammates into $NAME ──"

# --- required tooling ------------------------------------------------------
missing=()
for c in git python3 claude; do command -v "$c" >/dev/null || missing+=("$c"); done
if [ ${#missing[@]} -gt 0 ]; then
  echo "  MISSING (required): ${missing[*]}" >&2
  echo "  claude → https://claude.com/claude-code" >&2
  exit 1
fi
for c in node bun uv; do
  command -v "$c" >/dev/null || echo "  optional, not found: $c (claude-mem memory needs it)"
done

# git must be new enough for worktrees + --sparse. A 2019-era git shadowing the
# system one cost us 247 silent failures inside a harness.
gv="$(git --version | awk '{print $3}')"
if [ "$(printf '%s\n2.25.0\n' "$gv" | sort -V | head -1)" != "2.25.0" ]; then
  echo "  WARNING: git $gv is older than 2.25 — worktrees and --sparse will misbehave."
  echo "           the hooks prefix PATH=/usr/bin to dodge a shadowing install."
fi

# --- lay down the files ----------------------------------------------------
mkdir -p "$DEST"/{hooks,agents,docs,briefs,dashboard}
cp "$SRC"/core/hooks/*.py   "$DEST/hooks/"
cp "$SRC"/core/dashboard/*  "$DEST/dashboard/"       # the dashboard page: core-owned, upgraded like the hooks
# Seat specs are PROJECT-owned once installed: copy only those missing, with the
# real project name. Re-installs used to overwrite them, and the templates'
# literal ../PROJECT-ui made every project in one folder share one worktree.
# cto.md is the orchestrator's contract with these hooks: core owns it.
for f in "$SRC"/core/agents/*.md; do
  t="$DEST/agents/$(basename "$f")"
  if [ ! -f "$t" ] || [ "$(basename "$f")" = cto.md ]; then
    sed "s#\.\./PROJECT-#../$NAME-#g" "$f" > "$t"
  fi
done
cp "$SRC"/docs/*.md         "$DEST/docs/"
# Running a hook writes hooks/__pycache__/ (unless PYTHONDONTWRITEBYTECODE is set). In a
# project with no Python .gitignore that left .pyc files in every tree, and `git add -A` committed
# them (FOUND 2026-10-28 by CI: a board-only commit counted as a change of truth). Our own folder,
# our own ignore file: the project's .gitignore is left alone.
grep -qx 'hooks/__pycache__/' "$DEST/.gitignore" 2>/dev/null || echo 'hooks/__pycache__/' >> "$DEST/.gitignore"
chmod +x "$DEST"/hooks/*.py 2>/dev/null || true
echo "  hooks   : $(ls "$DEST"/hooks/*.py | wc -l | tr -d ' ')"
echo "  agents  : $(ls "$DEST"/agents/*.md 2>/dev/null | wc -l | tr -d ' ')"
echo "  docs    : $(ls "$DEST"/docs/*.md | wc -l | tr -d ' ')"

# --- config, preserved across upgrades -------------------------------------
if [ ! -f "$DEST/config.json" ]; then
  # The CTO runs on Claude Code's own default model until you name one (04-ROUTING).
  # Until 2026-10 this defaulted to an Ollama cloud model, which stopped answering on
  # 2026-09-24: a fresh install on a machine with ollama got a CTO that could not start.
  #
  # Production is the branch this repository already ships from, not an assumption:
  # a `master` project protected as `main` would protect nothing.
  PROD="$(git -C "$PROJECT" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##' || true)"
  if [ -z "$PROD" ]; then
    for b in main master; do
      git -C "$PROJECT" rev-parse --verify --quiet "refs/heads/$b" >/dev/null && { PROD="$b"; break; }
    done
  fi
  PROD="${PROD:-main}"
  # Product code: the usual source folders this project actually has.
  GUARDED=""
  for d in src lib app apps packages server api backend frontend; do
    [ -d "$PROJECT/$d" ] && GUARDED="$GUARDED\"$d/\", "
  done
  GUARDED="${GUARDED%, }"
  [ -n "$GUARDED" ] || GUARDED='"src/", "lib/", "app/"'
  cat > "$DEST/config.json" <<JSON
{
  "project": "$NAME",
  "cto": { "harness": "claude-code", "provider": "anthropic", "model": "default" },
  "protected_branches": ["$PROD", "development"],
  "guarded_paths": [$GUARDED]
}
JSON
  echo "  config  : created (production branch: $PROD, integration: development)"
  echo "            product code (guarded_paths): $GUARDED"
  echo "            check both in .ai-teammates/config.json before real work"
else
  echo "  config  : preserved"
fi

[ -f "$DEST/board.json" ] || cat > "$DEST/board.json" <<JSON
{ "updated": "$(date +%F)", "columns": ["todo","doing","review","done"],
  "agents": {}, "tasks": [] }
JSON

# --- register the hooks where each harness actually looks ------------------
# Our code lives in .ai-teammates/, but neither harness knows that. Claude Code
# reads .claude/settings.json and OpenCode reads opencode.json - those paths are
# their contract, not our choice. Registering in the wrong place is how a
# guardrail ends up passing its own self-test while protecting nothing.
python3 - "$PROJECT" <<'PY'
import json, re, sys
from pathlib import Path

proj = Path(sys.argv[1])
hooks = ".ai-teammates/hooks"
# "$CLAUDE_PROJECT_DIR"-anchored: a relative path fails from a subdirectory, and
# python exits 2 on a missing file - which PreToolUse reads as BLOCK.
run = 'python3 "$CLAUDE_PROJECT_DIR"/' + hooks


def load(p):
    """Never rewrite a file we could not parse. The old fallback was `{}`, so
    one JSONC comment wiped the project's whole settings file."""
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception as e:
        sys.exit(f"  ABORT: {p} is not plain JSON ({e}).\n"
                 f"         Not rewriting it. Fix or back it up, then re-run.")


# Claude Code — merge, never clobber: the project may have its own hooks.
sp = proj / ".claude" / "settings.json"
sp.parent.mkdir(exist_ok=True)
settings = load(sp)
op = proj / "opencode.json"
oc = load(op)          # parse both before writing either
# A list, not a dict: loopguard shares PostToolUse and UserPromptSubmit with
# activity.py. Each (event, command) is registered on its own.
# PostToolUseFailure and PermissionRequest are where failures and approval
# denials actually surface (PostToolUse only fires on success) - measured, not
# assumed: see loopguard.py.
wanted = [
    ("PreToolUse",         f"{run}/guard.py"),
    ("PostToolUse",        f"{run}/activity.py"),
    ("PostToolUse",        f"{run}/loopguard.py PostToolUse"),
    ("PostToolUseFailure", f"{run}/loopguard.py PostToolUseFailure"),
    ("PermissionRequest",  f"{run}/loopguard.py PermissionRequest"),
    ("UserPromptSubmit",   f"{run}/activity.py"),
    ("UserPromptSubmit",   f"{run}/loopguard.py UserPromptSubmit"),
    ("SessionStart",       f"{run}/session_start.py"),
    ("SessionEnd",         f"{run}/session_end.py"),
    # memlife: when a CTO turn ends with unreviewed commits on the integration branch, ask once
    ("Stop",               f"{run}/memlife.py stop-hook"),
]
h = settings.setdefault("hooks", {})
added, replaced = [], []
for event, cmd in wanted:
    entries = h.setdefault(event, [])
    script = cmd.rsplit("/", 1)[-1]          # e.g. guard.py

    # FOUND 2026-09-10: this used to check `if hooks not in flat`, i.e. whether
    # OUR path was already registered - not whether ANYTHING was already
    # handling the event. A project with the same scripts wired from an older
    # directory got both registered, so every guard check and activity log ran
    # TWICE. The check looked for its own string instead of for the condition,
    # which is the failure shape this whole system is about.
    #
    # Drop only a prior registration of THIS script (an older .claude/hooks or
    # .ai-teammates/hooks copy), one hook at a time. FOUND 2026-09-19: this
    # dropped whole entries for anything under .claude/hooks/, deleting the
    # project's own unrelated hooks - "merge, never clobber" in name only.
    ours = re.compile(r"(?:\.ai-teammates|\.claude)/hooks/" + re.escape(script) + r"\b")
    kept = []
    for e in entries:
        hs = e.get("hooks", [])
        keep = [hh for hh in hs
                if hh.get("command") == cmd or not ours.search(hh.get("command", ""))]
        if len(keep) != len(hs):
            replaced.append(event)
        if keep:
            e["hooks"] = keep
            kept.append(e)
    entries[:] = kept

    if not any(hooks in json.dumps(e) and script in json.dumps(e) for e in entries):
        entries.append({"hooks": [{"type": "command", "command": cmd}]})
        added.append(event)
sp.write_text(json.dumps(settings, indent=2))
print(f"  claude  : hooks registered in .claude/settings.json ({len(added)} added"
      + (f", {len(replaced)} duplicate(s) removed" if replaced else "") + ")")

# OpenCode — the deny-list is the real boundary for a seat.
perm = oc.setdefault("permission", {})
bash = perm.setdefault("bash", {})
bash.setdefault("*", "ask")
for k in ("git push*", "wrangler*", "supabase*", "ssh*", "scp*", "rsync*",
          "systemctl*", "curl*", "rm -rf*"):
    bash[k] = "deny"
edit = perm.setdefault("edit", {})
edit.setdefault("*", "allow")
for k in ("**/.env", "**/.env.*", ".ai-teammates/**", "**/secrets/**"):
    edit[k] = "deny"
perm.setdefault("external_directory", "deny")
perm.setdefault("webfetch", "deny")
op.write_text(json.dumps(oc, indent=2))
print("  opencode: deny-list written to opencode.json")
PY

# --- git pre-push hook -----------------------------------------------------
# The branch rules are enforced by git itself, not by pattern-matching commands
# inside one harness: pre-push is told where each ref really lands, fires in
# OpenCode, Codex, scripts and your terminal alike, and lives in the common git
# dir, so every worktree shares one copy.
HOOKS_DIR="$(cd "$PROJECT" && python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import _config; print(_config.hooks_dir())' "$DEST/hooks")"
HOOK="$HOOKS_DIR/pre-push"
if [ -f "$HOOK" ] && ! grep -q "AI-teammates pre-push" "$HOOK"; then
  echo "  WARNING: $HOOK exists and is not ours - left untouched. Pushes are NOT"
  echo "           guarded until it also runs (stdin must reach it):"
  echo '           python3 "$(git rev-parse --show-toplevel)/.ai-teammates/hooks/prepush.py" --hook "$@"'
else
  mkdir -p "$HOOKS_DIR"
  cat > "$HOOK" <<'SH'
#!/bin/sh
# AI-teammates pre-push: judge the refs git is actually pushing (see prepush.py).
f="$(git rev-parse --show-toplevel)/.ai-teammates/hooks/prepush.py"
[ -f "$f" ] || { echo "AI-teammates: $f missing - push blocked (fail-closed)" >&2; exit 1; }
exec python3 "$f" --hook "$@"
SH
  chmod +x "$HOOK"
  echo "  git     : pre-push hook installed in $HOOKS_DIR"
fi

# --- runtime state -------------------------------------------------------
# Events, runs, reviews and seat logs live in the common git dir now: shared by
# every worktree, never committed. Carry over what older installs wrote.
STATE="$(cd "$PROJECT" && python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import _config; print(_config.state_dir())' "$DEST/hooks")"
for f in events.jsonl agent_runs.jsonl reviews.jsonl; do
  if [ -f "$DEST/$f" ] && [ ! -f "$STATE/$f" ]; then
    cp "$DEST/$f" "$STATE/$f" && echo "  state   : migrated $f -> $STATE (the old copy can be deleted)"
  fi
done

# --- put `cto` on PATH -----------------------------------------------------
BIN="$HOME/.local/bin"; mkdir -p "$BIN"
ln -sf "$SRC/bin/cto" "$BIN/cto"
ln -sf "$SRC/install.sh" "$BIN/ai-teammates"
echo "  cto     : $BIN/cto -> $SRC/bin/cto"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "  PATH    : add this to your shell rc →  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

# --- memory ----------------------------------------------------------------
if [ ! -d "$HOME/.claude-mem" ]; then
  echo
  echo "  memory  : claude-mem not installed. Sessions will not be remembered."
  echo "            npx claude-mem install"
else
  echo "  memory  : claude-mem present"
fi

echo
# The guardrails are only inherited by new worktrees if they are ON the default
# branch. An uncommitted opencode.json protects the checkout you are standing in
# and nothing else — guardrail_test will (correctly) fail until this is done.
# Not committed automatically: this is the user's repo, not ours.
uncommitted="$(git -C "$PROJECT" status --porcelain -- .ai-teammates opencode.json .claude/settings.json .gitignore 2>/dev/null | wc -l | tr -d ' ')"
if [ "$uncommitted" != "0" ]; then
  # only paths that exist: `git add` refuses the whole command over one missing pathspec
  paths=""; for f in .ai-teammates opencode.json .claude/settings.json .gitignore; do
    [ -e "$PROJECT/$f" ] && paths="$paths $f"
  done
  echo "  COMMIT THESE — until you do, new worktrees start unguarded:"
  echo "    git add$paths"
  echo "    git commit -m 'chore: install AI-teammates'"
  echo
fi
# `cto` works from the integration branch's checkout (bin/cto). A project that has only
# its production branch gets the two commands that create it, instead of a refusal later.
read -r PROD_BR INT_BR <<< "$(cd "$PROJECT" && python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import _config; p, i = _config.branches(); print(p, i or "")' "$DEST/hooks")"
if [ -n "${INT_BR:-}" ] && ! git -C "$PROJECT" rev-parse --verify --quiet "refs/heads/$INT_BR" >/dev/null; then
  echo "  NEXT: create the integration branch '$INT_BR' and its own checkout (after committing):"
  echo "    git branch $INT_BR $PROD_BR"
  echo "    git worktree add ../$NAME-dev $INT_BR"
  echo
fi
echo "  Done. Start with:  cto --doctor    then:  cto"
echo "  First read: $DEST/docs/00-START-HERE.md"
