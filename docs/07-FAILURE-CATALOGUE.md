# The Failure Catalogue

Nine shapes. Each one recurred — that is why it is here. A failure that
happened once is bad luck; a failure that happened three times is a missing
guardrail.

The CTO reads this file at the start of every session. Not as history — as a
list of the specific ways it is about to be wrong.

---

## 1. A check pointed at a source that cannot see the thing — **9 times**

The most expensive shape in this catalogue, and the hardest to notice, because
a check like this *passes*. It manufactures confidence.

| Instance | The check | What it could not see |
|---|---|---|
| Live audit called "failed" | Read `agent_runs.jsonl` | That file only gets a row when a process **exits**. A running job is invisible. The user corrected it from a dashboard while it ran. |
| Guardrail test "passing" | Grepped `prepush.py` **source** for a string | Whether the code actually blocks anything at runtime |
| `api.py` "md5-verified in git" | Compared a file md5 to itself | That the file was in **no commit on any branch**. `git cat-file -t <that hash>` → "Not a valid object name". Five production files existed only on the server. |
| Hook features "measured" | Split transcripts on `\n` and `.!?` | Arabic speech-to-text returns neither. Every feature measured the whole transcript. One "3.69x lift" was actually 0.84x — refuted. |
| Secret rotation "complete" | Checked the VPS and Workers | `.env.local` was never updated. Local 401s for a day. |
| Scrape returned `scanned: 0` | `except: return []` | An HTTP 402. A billing outage looked like an empty result set for an hour. |
| Deploy "succeeded" | wrangler exit code | The bundle had no Supabase config baked in. Every visitor saw "Something broke". |
| Proxy "not an open relay" | Path allowlist | The allowlist restricts *which* endpoints, never *who*. Anyone on the internet could write to user data. |
| Seats "not safe to run" | Directory existence | The seats were as guarded as ever; their scratch directories had been cleaned up. |

**Rule.** Before trusting a check, ask: *can the thing I am reading physically
contain the fact I am asserting?* Prefer executing over grepping, the artifact
over the log, and the live system over your own notes.

**In this repo:** `guardrail_test.py` executes `prepush.py` against a real
commit rather than grepping it. `check_build_env.py` greps the **built bundle**,
not the config that fed it.

---

## 2. Editing the wrong function — **3 times**

`text.rindex("return {")` matched the *last* such string in a 900-line file
three separate times. Once it silently broke `POST /scout/seeds` for every
caller and left a cache permanently unwritten.

**Rule.** Anchor edits on unique surrounding context, never on a positional
match. After any structural edit, execute the changed path — do not re-read it
and conclude it looks right.

---

## 3. Verified once, then something moved

A cutover was verified, then a build baked the *old* key into the bundle at
build time. Both statements were true; the conclusion was false.

**Rule.** State *when* a verification was true. If a build step sits between
your check and the artifact users get, your check is upstream of the truth.

---

## 4. Field-name mismatches — **3 times**

`shortCode`/`code`, `videoViewCount`/`videoPlayCount`, `caption`/`caption_text`.
Each produced confident, plausible, wrong numbers. Because the code read a
missing key and got a default, nothing raised.

**Rule.** Print one raw record from any external source before writing the
parser. Build the parser around what you observed, not what the docs claim.

---

## 5. Incomplete rotation

A credential rotated in two of three places. The third failed quietly for a day.

**Rule.** Enumerate every location a secret lives *before* rotating any of them.
Rotation is done when the old value fails everywhere, not when the new one works
somewhere.

---

## 6. Silent failure dressed as success

`except: return []`. `DEFAULT_PROFILE_LIMIT = 50` produced 454 requests, hit a
data ceiling, aborted without writing results, still billed $0.58, and reported
**SUCCEEDED** with zero output.

**Rule.** Never catch an exception you cannot describe. A vendor "success"
status is not evidence of output — check the output.

---

## 7. Stale docs mislead agents exactly like people

A deploy doc named a branch that had not existed for eleven days. An error
message told users to "Connect Supabase in Lovable Cloud" two weeks after
Lovable was removed — the one message a broken deploy shows, pointing at a
product that was gone. A routing table listed a model `ollama list` does not
have.

**Rule.** When a decision changes, every doc claiming otherwise gets a dated
SUPERSEDED banner **in the same commit**. Agents cannot tell a stale doc from a
current one; they will act on both.

---

## 8. A guardrail that cannot be satisfied gets overridden by habit

Branch protection compared `git diff upstream..HEAD`, which cannot distinguish a
file that arrived by *merging a reviewed branch* from one committed directly.
The integration branch exists to accumulate merges — so it was permanently
unpushable. It reported 32 violations where 6 were real. People overrode it, and
then a genuine violation went through unnoticed.

**Rule.** A rule nobody can satisfy is worse than no rule. If a guardrail fires
on correct behaviour, fix the guardrail the same day. Overrides must be
announced with the file and the reason.

---

## 9. The orchestrator declaring a run dead — twice

Once by reading a log that only fills on exit; once by reading the first 60
lines of a log and mistaking startup **warnings** for a fatal error while the
process was answering correctly. Both nearly caused a second concurrent run of
an expensive job.

**Rule.** Liveness is a process-table question. `pgrep`, elapsed time, CPU.
Never a log file, never a warning line.

---

## Meta-lesson

Eight of these nine are the same failure wearing different clothes: **a
confident statement whose evidence could not have supported it.** Not one was
caused by a model being insufficiently intelligent.

The remedy is not a better model. It is making the evidence check the real
thing, and saying out loud what you actually verified versus what you assume.
