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

export HOME="$WORK/home" GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
mkdir -p "$HOME" "$WORK/bin"
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
git branch development
git worktree add -q "$WORK/project-dev" development

echo
echo "── guardrail suite ──"
cd "$WORK/project-dev"
python3 .ai-teammates/hooks/guardrail_test.py
