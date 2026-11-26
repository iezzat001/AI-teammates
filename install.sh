#!/usr/bin/env bash
# Install AI-teammates into a project.
#
#   /path/to/AI-teammates/install.sh ~/your-project      point it at the project, from anywhere
#   cd ~/your-project && /path/to/AI-teammates/install.sh
#
# Copies the hooks, agent specs and docs into <project>/.ai-teammates, writes a
# config, puts `cto` on your PATH, and leaves the project's own files alone.
# A first install from a terminal goes straight on into `cto setup`, which asks
# its questions, commits the install and creates the development checkout.
#
#   install.sh <project> --yes               setup with every default (scripts, CI)
#   install.sh <project> --answers a.json    setup from a file
#   install.sh <project> --no-setup          only install
#
# Re-running upgrades the hooks and docs and preserves board.json and config.

set -euo pipefail

# python3 resolves our own path just below, so it is checked before anything uses it.
command -v python3 >/dev/null || { echo "python3 is required (3.9 or newer)." >&2; exit 1; }

# Resolve symlinks: `ai-teammates` on PATH is a link to this file, and dirname
# of the LINK is ~/.local/bin - so every `ai-teammates install` failed at cp.
SRC="$(dirname "$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "${BASH_SOURCE[0]}")")"
# Your own git, unless it is too old to work with and a newer one is installed (_config.tool_path).
# Until 2026-11 /usr/bin was put first here for everybody.
NEWPATH="$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import _config; print(_config.tool_path())' "$SRC/core/hooks" 2>/dev/null || true)"
[ -n "$NEWPATH" ] && export PATH="$NEWPATH"

usage() { echo "usage: ai-teammates [install] [<project folder>] [--yes | --answers file.json | --no-setup]
       ai-teammates setup | doctor | uninstall        (from inside the project)" >&2; exit 2; }
MODE="${1:-install}"
case "$MODE" in
  install|uninstall) [ $# -gt 0 ] && shift ;;
  doctor)  exec "$SRC/bin/cto" --doctor ;;
  setup)   exec "$SRC/bin/cto" "$@" ;;
  -*)      MODE=install ;;                 # options only: install here
  *)       [ -d "$MODE" ] || usage         # a folder: install into it
           MODE=install ;;
esac
# Until 2026-11 the installer only worked from inside the project. `install.sh <project>` is
# how a new user says it: clone AI-teammates, point it at the project.
if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then
  [ -d "$1" ] || { echo "no such folder: $1" >&2; exit 1; }
  cd "$1"; shift
fi
# What is left are options for `cto setup`, which follows the install when there is someone to
# ask (a terminal) or the answers were given (--yes, --answers). --no-setup: only install.
NO_SETUP=""; SETUP_ARGS=()
for a in "$@"; do
  if [ "$a" = "--no-setup" ]; then NO_SETUP=1; else SETUP_ARGS+=("$a"); fi
done
[ "$MODE" = uninstall ] && [ ${#SETUP_ARGS[@]} -gt 0 ] && usage

PROJECT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "$(pwd) is not inside a git repository. In the project folder: git init, make a first commit, then run this again." >&2; exit 1; }
DEST="$PROJECT/.ai-teammates"
NAME="$(basename "$PROJECT")"

if [ "$MODE" = uninstall ]; then
  # `rm -rf .ai-teammates` (the old instructions) left the hooks registered: a
  # missing script makes python exit 2, which Claude Code's PreToolUse reads as
  # BLOCK - every tool call in the project refused. Unregister first.
  python3 "$SRC/core/hooks/codex_hooks.py" uninstall "$PROJECT"
  HOOK="$(cd "$PROJECT" && python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import _config; print(_config.hooks_dir())' "$DEST/hooks")/pre-push"
  grep -q "AI-teammates pre-push" "$HOOK" 2>/dev/null && rm "$HOOK" && echo "  git     : pre-push hook removed"
  rm -rf "$DEST" && echo "  removed : .ai-teammates/ (board, facts and config are in git history)"
  echo "  kept    : ~/.local/bin/cto (shared by other projects)"
  exit 0
fi

echo "── installing AI-teammates into $NAME ──"

# --- required tooling ------------------------------------------------------
missing=()
for c in git python3 "${AI_TEAMMATES_CODEX_BIN:-codex}"; do command -v "$c" >/dev/null || missing+=("$c"); done
if [ ${#missing[@]} -gt 0 ]; then
  echo "  MISSING (required): ${missing[*]}" >&2
  echo "  codex → https://developers.openai.com/codex/cli" >&2
  exit 1
fi
for c in node bun uv; do
  command -v "$c" >/dev/null || echo "  optional, not found: $c (claude-mem memory needs it)"
done

# git must be new enough for worktrees + --sparse. A 2019-era git shadowing the
# system one cost us 247 silent failures inside a harness.
gv="$(git --version | awk '{print $3}')"
if [ "$(printf '%s\n2.25.0\n' "$gv" | sort -V | head -1)" != "2.25.0" ]; then
  echo "  WARNING: git $gv is older than 2.25 and no newer one was found — worktrees will misbehave."
  echo "           Install a current git and run this again."
fi

# --- lay down the files ----------------------------------------------------
mkdir -p "$DEST"/{hooks,agents,docs,briefs,dashboard}
cp "$SRC"/core/hooks/*.py   "$DEST/hooks/"
cp "$SRC"/core/dashboard/*  "$DEST/dashboard/"       # the dashboard page: core-owned, upgraded like the hooks
# Seat specs are PROJECT-owned once installed: copy only those missing, with the
# real project name. Re-installs used to overwrite them, and the templates'
# literal ../PROJECT-ui made every project in one folder share one worktree.
# cto.md is the orchestrator's contract with these hooks: core owns it.
# A seat the owner dropped (`cto setup` moves it to agents/off/) stays dropped.
for f in "$SRC"/core/agents/*.md; do
  t="$DEST/agents/$(basename "$f")"
  [ -f "$DEST/agents/off/$(basename "$f")" ] && continue
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
FRESH=""
if [ ! -f "$DEST/config.json" ]; then
  FRESH=1
  # Default CTO route for a new Codex project.
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
  "cto": { "model": "gpt-6.2-astra", "effort": "xhigh" },
  "protected_branches": ["$PROD", "development"],
  "guarded_paths": [$GUARDED],
  "delegation": { "cto_product_edits": "block" }
}
JSON
  echo "  config  : created (production branch: $PROD, integration: development)"
  echo "            product code (guarded_paths): $GUARDED"
  echo "            delegation: block (the CTO writes cards; seats write the product code)"
  echo "            check these in .ai-teammates/config.json, or run: cto setup"
else
  echo "  config  : preserved"
fi

[ -f "$DEST/board.json" ] || cat > "$DEST/board.json" <<JSON
{ "updated": "$(date +%F)", "columns": ["todo","doing","review","done"],
  "agents": {}, "tasks": [] }
JSON

python3 "$DEST/hooks/codex_hooks.py" install "$PROJECT"
echo "  codex   : hooks registered in .codex/hooks.json; review them in Codex with /hooks"
# --- git pre-push hook -----------------------------------------------------
# The branch rules are enforced by git itself, not by pattern-matching commands
# inside one harness: pre-push is told where each ref really lands, fires in
# every tool, script and terminal alike, and lives in the common git dir, so
# every worktree shares one copy.
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
  echo "  memory  : claude-mem not installed. Historical Claude memory is unavailable."
  echo "            npx claude-mem install"
else
  echo "  memory  : claude-mem present"
fi

echo
# Setup next, when it can run: it asks the questions the installer does not, commits the install
# and creates the integration branch and its checkout. On a first install with a terminal, or
# whenever its options were given. An upgrade from a terminal does not ask everything again.
if [ -z "$NO_SETUP" ] && { [ ${#SETUP_ARGS[@]} -gt 0 ] || { [ -n "$FRESH" ] && [ -t 0 ] && [ -t 1 ]; }; }; then
  echo "  Installed. Now the setup: \`cto setup\` (run it again any time to change a choice)."
  cd "$PROJECT"
  exec "$SRC/bin/cto" setup ${SETUP_ARGS[@]+"${SETUP_ARGS[@]}"}
fi

# The guardrails are only inherited by new worktrees if they are ON the default
# branch. An uncommitted hook registration protects the checkout you are standing
# in and nothing else — guardrail_test will (correctly) fail until this is done.
# Not committed here: `cto setup` offers to, and otherwise it is these two lines.
uncommitted="$(git -C "$PROJECT" status --porcelain -- .ai-teammates .codex/hooks.json .claude/settings.json .gitignore 2>/dev/null | wc -l | tr -d ' ')"
if [ "$uncommitted" != "0" ]; then
  # only paths that exist: `git add` refuses the whole command over one missing pathspec
  paths=""; for f in .ai-teammates .codex/hooks.json .claude/settings.json .gitignore; do
    [ -e "$PROJECT/$f" ] && paths="$paths $f"
  done
  echo "  NOT COMMITTED YET — until it is, new worktrees start unguarded. \`cto setup\` offers to, or:"
  echo "    git add$paths"
  echo "    git commit -m 'chore: install AI-teammates'"
  echo
fi
# `cto` works from the integration branch's checkout (bin/cto), never on production.
read -r PROD_BR INT_BR <<< "$(cd "$PROJECT" && python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import _config; p, i = _config.branches(); print(p, i or "")' "$DEST/hooks")"
if [ -n "${INT_BR:-}" ] && ! git -C "$PROJECT" rev-parse --verify --quiet "refs/heads/$INT_BR" >/dev/null; then
  echo "  The integration branch '$INT_BR' and its checkout do not exist yet: \`cto setup\` creates them."
  echo
fi
echo "  Done. Next, in $PROJECT:"
echo "    cto setup    the tool and plan, memory, team, scope and spend; it commits the install,"
echo "                 creates the '${INT_BR:-development}' checkout, and ends with cto --doctor"
echo "    cto          in that checkout"
echo "  First read: $DEST/docs/00-START-HERE.md"
