# Security policy

## Reporting a vulnerability

Please **don't open a public issue** for a security problem, including a way around a
guardrail.

Report it privately through GitHub:
[Security → Report a vulnerability](https://github.com/iezzat001/AI-teammates/security/advisories/new).
Include the exact input that triggers it, what happens versus what should happen, and the
narrowest fix you can see.

You'll get an acknowledgement within 7 days. Fixes go into `main`; there are no
maintained release branches yet.

## What the guardrails protect against

AI-teammates assumes a **cooperative but error-prone agent**: one that follows its
instructions, makes mistakes, and, after a refusal, tends to retry the same thing spelled
differently. The guardrails are built to catch that agent, and to make its mistakes visible:

- product code committed straight to a protected branch, or pushed there (git pre-push hook)
- a teammate editing files outside its task card (checked live by a hook, and again from
  the git diff before anything is committed)
- a teammate running commands its card did not grant, or the project's denied commands
- an agent retrying something only a human can fix (approvals, logins, billing)
- a "done" claim with nothing behind it (the runner executes the acceptance commands)

## What they don't protect against

They are **not a sandbox** and won't stop a determined or compromised agent:

- **Hooks run on your machine with your permissions.** So do the agents.
- **Acceptance commands run as you.** `card.py check` refuses a card whose acceptance
  command directly runs a file the teammate may edit (`sh scripts/check.sh`, `node x.js`),
  or a `package.json`/`Makefile` script when the teammate may edit that file. It can't see
  inside a test runner: `npm test` or `pytest` will run test files the teammate edited. Read
  a card's acceptance commands before you run it.
- **Git hooks are client-side.** Anyone with a shell can skip them. Use your git host's
  branch protection for anything that must hold.
- **The command checks match command text.** They catch common spellings, not every
  possible one.
- **The independent-review gate checks that an audit ran, not what it found.** A promotion
  that touches auth or spend needs a finished read-only audit run newer than the change; the
  verdict is yours to read.
- **Teammates use your Claude account** and spend from the same limits as your own session.

If you find a gap in any of the protections listed above, that is a vulnerability: please
report it as described at the top of this page.
