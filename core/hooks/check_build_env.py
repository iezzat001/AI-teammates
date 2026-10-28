#!/usr/bin/env python3
"""Refuse to deploy a bundle built without its build-time config.

Build-time vars (VITE_*, NEXT_PUBLIC_*, ...) are baked into the browser bundle
when it is built. .env is gitignored, so per-worktree: a build from a worktree
that never got one deploys "successfully" and fails for every visitor. Worker or
server secrets do not save it. So this looks at the ARTIFACT, not the config.

Configure in .ai-teammates/config.json:

    "build_env": {"required": ["VITE_SUPABASE_URL", "VITE_SUPABASE_PUBLISHABLE_KEY"],
                  "artifact": ".output/public"}

The value of the FIRST required var must appear literally in the built JS.

    python3 .ai-teammates/hooks/check_build_env.py      # after the build

FOUND 2026-09-19: it resolved the repo as parents[1] - `.ai-teammates/` once it
moved out of scripts/ - so it looked for `.ai-teammates/.env` and blocked every
build, with correct config sitting at the root. It also hardcoded one project.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _config  # noqa: E402

REPO = _config.repo_root()
CFG = _config.load().get("build_env") or {}
REQUIRED = CFG.get("required") or []


def fail(msg: str) -> None:
    print(f"  BLOCKED: {msg}")
    sys.exit(1)


if not REQUIRED:
    print("  build env: not configured (config.json build_env.required) — nothing to check")
    sys.exit(0)

env = REPO / ".env"
if not env.exists():
    fail(f"no .env in {REPO.name} — a build here bakes an unusable bundle.\n"
         f"  fix: copy .env from the main checkout into {REPO}")

text = env.read_text()
missing = [k for k in REQUIRED if not re.search(rf"^{re.escape(k)}=.+", text, re.M)]
if missing:
    fail(f".env is missing {', '.join(missing)} — these are what reach the browser.")

out = REPO / CFG.get("artifact", "dist")
if not out.exists():
    print(f"  .env looks right; no build at {out.name}/ to check yet.")
    sys.exit(0)

probe = re.search(rf"^{re.escape(REQUIRED[0])}=(.+)$", text, re.M).group(1).strip().strip("\"'")
hits = 0
for js in out.rglob("*.js"):
    try:
        if probe in js.read_text(errors="ignore"):
            hits += 1
    except Exception:
        continue

if hits == 0:
    fail(f"the built bundle does not contain {REQUIRED[0]} — build-time vars were "
         f"not substituted. Rebuild from a worktree that has .env.")

print(f"  build env OK: {REQUIRED[0]} baked into {hits} bundle file(s)")
