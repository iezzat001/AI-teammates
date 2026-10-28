# Bootstrapping an unfamiliar project

The CTO's first session on a new codebase. This is the least proven part of the
system — treat it as a procedure under test, not a solved problem.

---

## Rule zero: read before you write

A new project's conventions are not yours. The most damaging thing an agent can
do on day one is impose a layout the team did not choose.

**First session is read-only.** Produce a written understanding, get it
corrected, and only then touch anything.

---

## Session 1 — survey

The CTO reports:

1. **What is this?** Language, framework, entry points, how it runs locally,
   how it deploys. From the code and CI config — not from the README, which is
   the file most likely to be stale.
2. **Branch reality.** Which branches exist, which are protected, how work
   actually merges. Compare the documented flow with `git log --first-parent`.
   They diverge more often than not.
3. **Where the danger is.** What costs money per call. What holds user data that
   cannot be regenerated. What deploys to production and how.
4. **What guardrails already exist.** CI, hooks, branch protection, review
   requirements. Do not duplicate them.
5. **The stale-doc sweep.** Docs naming tools, branches or services that no
   longer exist. This is worth doing on day one because agents cannot tell a
   stale doc from a current one and will act on both with equal confidence.

Deliverable: one page. The human corrects it. **Corrections are the most
valuable output of session 1** — they are the project knowledge no file
contains.

---

## Session 2 — propose

Based on the survey and the corrections:

- **Which roles this project actually needs.** A data pipeline needs different
  teammates than a mobile app. Do not instantiate all four by default.
- **`guarded_paths`** matching the real layout.
- **Which existing conventions to keep.** If the team uses PR review, the
  guardrails complement it rather than replace it.
- **Which rules from the catalogue apply.** Most do; some do not. A project with
  no paid APIs does not need the spend rules.

Deliverable: a diff to `config.json` and a proposed team, for approval.

---

## Session 3 — one small task, end to end

Not a real feature. A **rehearsal** to prove the machinery:

1. CTO delegates something trivial to one teammate
2. Teammate works in its own worktree
3. CTO reviews using the protocol in [05-CTO](05-CTO.md)
4. It lands via a feature branch
5. `prepush` is exercised — confirm it **blocks** when it should

If any step surprises you, fix it now. A rehearsal failure is cheap; the same
failure during real work is not.

---

## What does not transfer

Be explicit with the CTO about this, because a confident agent will generalise
from the docs it was given:

- **The routing table's measurements** were taken on one codebase. The
  harness gotchas transfer; the model rankings should be re-measured.
- **The specific incidents** are examples of *shapes*, not predictions. Your
  project will have its own. The catalogue teaches you what a repeating failure
  looks like, not which one you will get.
- **The four-worktree layout** suits a small team on one machine. A larger team
  with real CI may need none of it.

---

## Success criteria

You will know it worked when the CTO tells you something about your project that
you did not know and that turns out to be true — a stale doc, an unguarded
endpoint, a file that exists in no commit.

That has been the pattern: the survey step finds something real. If session 1
produces only a tidy summary of what you already knew, the CTO is reading docs
instead of the system.
