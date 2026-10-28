# Routing: which seat gets the card, on which model

One harness, one choice per card.

**Harness: Claude Code, for the CTO and every seat.** Hooks only fire in Claude
Code, and the hooks are where this system's guarantees live: edits outside a card's
scope are denied while the seat works, a human-only blocker ends the run instead of
looping, and the run has a wall-time and tool-call budget. A harness without hooks
can only be checked after the fact. (OpenCode and Codex were tried; see History.)

**The choice that is left is the model.** The CTO's is fixed in config; a seat's is chosen per
card by `tier`. Decided 2026-10-04 with the owner: the CTO runs on the strongest model at
maximum effort, because judgement, decomposition and review are its whole job and it is the one
agent that can make every other run wasted; the seats run on the cheapest model that can do the
card, because a card is small by construction and its result is verified by executed checks.

| Who | Use it for | Model (config) |
|---|---|---|
| CTO | the session you talk to: plans, writes cards, reviews, merges | `claude-opus-5-5`, effort `max` (`cto.model`, `cto.effort`) |
| card `mechanical` | rewording, renames, boilerplate, applying a known pattern | `claude-haiku-4-5-20251001` (`seat_models`) |
| card `standard` | a component or endpoint with a clear spec | `claude-sonnet-5-5` |
| card `hard` | reasoning, algorithms, security-critical code: correctness is the deliverable | `claude-opus-5-5` |
| free-form read-only seat | audits (`seat.py run security-audit "..."`) | `claude-sonnet-5-5` (the spec's `model:`) |

An audit runs on every change that decides access or spend, so its cost recurs: Sonnet 5.5 by
default. For one change where a missed bug is expensive, write the audit as a `hard`, read-only
card instead and it runs on Opus 5.5.

`card.model` overrides the tier for one card. Verified to resolve through `claude --model` on
2026-10-04: `claude-haiku-4-5-20251001`, `claude-sonnet-5-5`, `claude-opus-5-5` (and on
2026-10-02 the older `claude-sonnet-5`, `claude-opus-5`). `claude-fable-5-1` answered "out of
usage credits" on this account, so it is not used. Confirm an id resolves before putting it in a
config, and record what you find on the board.

**What a session carries matters more than the model's price.** Everything in context is
re-sent with every call of a session. Measured 2026-10-05 on the owner's CTO sessions: a ~200k
token baseline before any work, of which ~100k were the tool definitions of MCP servers inherited
from the user's setup (used twice in three days) and ~24k the whole `board.json` (77 of 100
cards closed); a 111-call session re-read 35.7M tokens from cache. So the CTO now starts with no
MCP servers (`cto.mcp`: `none` by default, `inherit`, or a path to an MCP config file), its brief
carries only the open board cards (`board.py show <ID>` for one in full), and the guardrails and
routing docs are read when needed. Replaying the startup on a copy of ContentOS: 138k -> 38k
tokens of context, $0.48 -> $0.05 (Haiku; the ratio is what carries over).

Skills cost the same way: every skill's description is listed in every call. A project hides what
it does not need without touching other projects: `"enabledPlugins": {"<plugin>@<marketplace>":
false}` in its `.claude/settings.json` (project settings outrank user settings; `install.sh` keeps
the key) and `"skillOverrides": {"<skill>": "user-invocable-only"}` for user-level skills (hidden
from the model, `/name` still works for the human). On ContentOS, Cloudflare's 28 skill entries
cost 4,174 tokens per Opus 5.5 call (2026-10-06). Measure on the model that will run: Claude Code
caps the skill listing for smaller context windows, so on Haiku the same change looked like 460.

**Tools cost most of all.** A session is offered 24 built-in tools by default, with a skill listing
and a subagent listing that exist only for the Skill and Task tools. The CTO used 7 tools in ~400
calls over six sessions (and TaskStop once), never a skill or a subagent. Naming its working set
(`cto.tools`, default `Bash,Read,Edit,Write,Glob,Grep,WebFetch,WebSearch,TaskStop`, `"default"` for
all) cut a one-word Opus 5.5 call from 26,601 to 9,942 tokens (2026-10-06); typed `/commands` still
work for the human. `--tools` takes a list, so the launcher puts it LAST, after the prompt. Seats
are offered exactly the tools their allowlist names (`cardrun.card_command`), which halved a card's
cost floor on Haiku ($0.058 -> $0.026), and the read-only audit seat runs with
`--strict-mcp-config --tools Read,Glob,Grep,Bash`: it had been inheriting every MCP server of the
owner's setup, write-capable ones included. `cto.claude_md_excludes` keeps a personal global
`CLAUDE.md` that says nothing about the codebase out of CTO sessions (2,181 tokens per call for
ContentOS's owner).

**Route by task value.** A strong model earns its cost on an audit that finds a real
bug. It does not earn it on rewording three strings. When in doubt, start one tier
lower and let the acceptance commands tell you: a card that fails its acceptance on
`mechanical` is a data point, not a failure of the routing.

**Cost is recorded, not guessed.** Every card run stores Claude Code's own
`total_cost_usd`; `seat.py report` sums it per seat. The harness's own prompt sets a floor per
run, measured on a one-word reply on 2026-10-04: Haiku 4.5 $0.058, Sonnet 5.5 $0.082, Opus 5.5
$0.163. The 5.0 models they replaced were dearer (Sonnet 5 $0.13, Opus 5 $0.21 on 2026-10-02), so
the move to 5.5 is cheaper as well as newer. A card should be worth its floor: below it, do it
yourself. Seats bill the same account as your own session, so a long audit also spends your
session limit (an 8-minute audit ran it out on 2026-10-03).

---

## Choosing for a task that does not fit a row

1. **Does it need hooks to fire?** Claude Code. Not negotiable.
2. **Does it write product code?** A card, in its own worktree, on a `writes: true`
   seat. Never the main checkout.
3. **Is it read-only?** A `read_only: true` seat (plan mode cannot write at all).
   Prefer this for audits: a read-only agent cannot corrupt what it misunderstands.
4. **Is correctness or cost the binding constraint?** Correctness: `hard`. Cost: `mechanical`.

Then write the choice on the card. A routing decision nobody wrote down gets
re-litigated every session.

---

## Harness gotchas that have cost real time

- **`--allowedTools` is variadic.** A prompt after it is parsed as tool patterns.
  Put the prompt **first** (the card runner does).
- **`nohup ... &` is not enough** to survive the launcher: it blocks SIGHUP but does
  not detach the process group. Use `detach.py` (double-fork through `os.setsid()`).
  Three runs died silently before this was understood.
- **macOS has no `timeout`.** Budgets are enforced in Python (`cardrun.supervise`).
- **`claude -p` warns about stdin** unless it is redirected (`< /dev/null`).
- **`--output-format stream-json` needs `--verbose`**, and its final `result` event is
  where the cost, turn count and any `permission_denials` live.
- **A permission denial does not fire `PostToolUseFailure`.** It surfaces on
  `PermissionRequest`; a command that runs and fails surfaces on `PostToolUseFailure`.
  (Measured 2026-10-02; this is why `loopguard.py` listens to both.)
- **`continue:false` from a failure hook does not stop a run.** A `PermissionRequest`
  deny with `interrupt:true` does.
- **Always verify a delegated run's output landed**: the runner computes the diff from
  git and executes the acceptance commands, but you still re-derive one claim.

---

## History: SUPERSEDED 2026-10-02

Until 2026-09-25 the seats ran on Ollama cloud models through OpenCode and Codex.
Ollama began answering 402 on 2026-09-24, the seats were moved to direct Anthropic
routes, and OpenCode's Anthropic route was never connected, which left two of three
code seats unable to run. The measurements are kept because they are real, but they
describe a setup that is no longer in use, and rankings from one codebase do not
transfer to another.

- OpenCode ignores the parent process cwd and needs `--dir`, or it writes into the
  wrong repo (this made early delegated runs look like no-ops). It has no turn cap, no
  hooks, and an `ask` rule auto-rejects headless.
- Codex stalled twice on documentation tasks, producing a confident report and no diff.
- kimi-k3 used ~4x the tokens of glm-5.2 and found three real bugs across three audits;
  deepseek-v4-pro needed ~280s per hard call; minimax-m3 was the vision option.
- A routing doc once named `glm-5.3-flash`, which `ollama list` never had. Verify that a
  model resolves before writing it into a config.
