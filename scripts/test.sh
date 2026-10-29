#!/usr/bin/env bash
# Run the guardrail suite the way a user's project runs it: install this checkout into a
# throwaway project and run guardrail_test.py there, from the integration branch's checkout.
#
#   scripts/test.sh            # one to two minutes; ends with "all guardrails in force"
#   KEEP=1 scripts/test.sh     # keep the temp project for debugging
#
# Nothing outside the temp folder is touched: HOME points into it (install.sh links `cto`
# into $HOME/.local/bin), and git ignores your global config. `claude` is a stub that reports
# "signed in", so no account is needed; the suite's own sign-in check proves nothing here.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ai-teammates-test.XXXXXX")"
cleanup() { [ -n "${KEEP:-}" ] && echo "kept: $WORK" || rm -rf "$WORK"; }
trap cleanup EXIT

REAL_HOME="$HOME"
export HOME="$WORK/home" GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
mkdir -p "$HOME" "$WORK/bin"
# MemPalace's vector store keeps its embedding model (~80 MB) under ~/.cache/chroma. With a fresh
# HOME every run downloaded it again, and a slow download timed the MemPalace checks out.
if [ -d "$REAL_HOME/.cache/chroma" ]; then
  mkdir -p "$HOME/.cache" && ln -s "$REAL_HOME/.cache/chroma" "$HOME/.cache/chroma"
fi
cat > "$WORK/bin/claude" <<'SH'
#!/bin/sh
case "$1" in
  auth)      echo '{"loggedIn": true}' ;;
  --version) echo "0.0.0 (test stub)" ;;
  *)         echo "claude test stub: no model in scripts/test.sh" >&2; exit 1 ;;
esac
SH
chmod +x "$WORK/bin/claude"
export PATH="$WORK/bin:$PATH"

proj="$WORK/project"
mkdir -p "$proj/src"
cd "$proj"
git init -q -b main
git config user.email test@example.com
git config user.name test
echo "export const a = 1" > src/a.ts
echo "# test project" > README.md
git add -A && git commit -qm "initial"

echo "── install ──"
"$SRC/install.sh"
git add -A && git commit -qm "chore: install AI-teammates"
# An upgrade of an installed project must change only what changed. FOUND 2026-10-28: every re-run
# rewrote .claude/settings.json without its final newline - a diff on every upgrade of a project whose
# file ends in one, as files saved by an editor or by Claude Code do. So: give it one, then re-install.
tail -c1 .claude/settings.json | od -An -c | grep -q '\\n' || { echo >> .claude/settings.json; git commit -qam "editor: final newline"; }
"$SRC/install.sh" > /dev/null
if [ -n "$(git status --porcelain)" ]; then
  echo "FAIL: re-running install.sh on an installed project changed:"; git status --porcelain; exit 1
fi
echo "  ok    re-running install.sh changes nothing"
# A seat the owner dropped in `cto setup` (moved to agents/off/) must stay dropped on an upgrade.
mkdir -p .ai-teammates/agents/off && mv .ai-teammates/agents/backend.md .ai-teammates/agents/off/
"$SRC/install.sh" > /dev/null
if [ -e .ai-teammates/agents/backend.md ]; then echo "FAIL: re-running install.sh brought back a dropped seat"; exit 1; fi
mv .ai-teammates/agents/off/backend.md .ai-teammates/agents/ && rmdir .ai-teammates/agents/off
echo "  ok    re-running install.sh leaves a dropped seat dropped"
git branch development
git worktree add -q "$WORK/project-dev" development

echo
echo "── guardrail suite ──"
cd "$WORK/project-dev"
python3 .ai-teammates/hooks/guardrail_test.py
