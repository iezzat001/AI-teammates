## What changes

<!-- One or two sentences. Link the issue: Fixes #... -->

## Why

<!-- The problem or incident behind it. For cost or speed changes: the numbers and how you measured them. -->

## How it is checked

<!-- The check in guardrail_test.py that executes this, and how you saw it fail without the fix. -->

## Checklist

- [ ] `scripts/test.sh` ends with `all guardrails in force`
- [ ] A new or changed guard has a test that runs it, and I saw that test fail without the fix
- [ ] Docs that this changes are updated (stale ones get a dated SUPERSEDED note)
- [ ] CHANGELOG.md has a line under Unreleased
