# Task 46 — the campaign's question, as a file

`end_to_end.py` is the script. `run/` is its output, including `run/prompt.txt`:
the exact SYSTEM and USER messages a model would receive, with the hypothesis in
them, so that claim is a file somebody can read rather than an assertion.

**N = 5 on synthetic data, and it supports no claim about the hypothesis.**
`MIN_SESSIONS` alone is 30 and the gate has no per-candidate nulls at that size.
What the run establishes is narrower: that a human's question reaches the agent's
input, is hashed into the pre-registration, and survives to a tier-3 verdict.

---

## The finding that matters most: nothing went stale

The brief said to expect the boundary to cost something — "every committed ledger
and campaign row's recorded fingerprint goes stale", inherited from Task 44's
framing. **It did not, and the reason is worth writing down, because the belief
is load-bearing in two briefs and it is wrong.**

`ledger_check.py` does not compare a row's `code_fingerprint` against the current
code. It compares it against the fingerprint **recomputed from the tree at the
commit the row itself names**, via `git archive <sha>` into a scratch directory:

```python
def code_fingerprint_at(sha: str) -> str | None:
    archive = subprocess.run(["git", "archive", sha], ...)
```

That comparison never touches HEAD or the working tree. So editing `src/core/**`
cannot stale a committed row: the row names a commit, the commit still produces
the fingerprint the row recorded, and the row stays checkable forever — as long
as history is not rewritten.

Measured after the boundary commit:

| ledger | rows | `code_fingerprint_matches` False |
|---|---|---|
| `runs/ledger.jsonl` | 5 | 1 |
| `runs/campaigns/task36/history/` | 100 | **0** |
| `runs/campaigns/task36/control/` | 100 | **0** |
| `runs/campaigns/ba2e91f668d6/` | 200 | **200** |

`ledger_check` on the main ledger: **N = 0, superseded 0, environment-drifted 0**
— identical to before the change. `campaign_summary --check`: **current** on both
Task 36 arms, stored equal to rebuilt. **No supersession record was written,
because none is warranted.** Writing one would have been worse than useless: the
flag removes a row from N, and these rows are not invalid.

**What this means for the next task that wants to cross the boundary.** It is
cheaper than the briefs say. The cost is not "every row goes stale"; it is "a row
run against *uncommitted* edits can never be pinned", which is a different and
much narrower hazard, and it is about discipline at run time rather than about
editing `src/core` afterwards.

## A pre-existing finding, surfaced by looking

**All 200 rows of `ba2e91f668d6` are unpinned, and were before this task
touched anything.** That is the Task 30 null campaign — 200 edgeless proposals on
the synthetic training split, the campaign the correction machinery was first
checked against, written 2026-09-14.

Their recorded `code_fingerprint` does not match the tree at the commit they
name, which means they were run against a working tree with uncommitted edits.
The code that produced them is not recoverable from history. They are not wrong
and they replay from stored source, but *what they were run under* cannot be
established, so any number derived from them carries that caveat.

This is unrelated to Task 46 and is not caused by it — `code_fingerprint_at` is
independent of the working tree, so the result is the same at any commit. It is
reported here because this is where somebody finally looked. It is also a good
argument for running `ledger_check` over campaign ledgers as a gate *before* a
campaign, which is what its own docstring says it is for and which nothing
currently does: the standard verification only ever points it at
`runs/ledger.jsonl`.

## The lint, measured

Run over the hypothesis prose already in the repo, none of which was written
with knowledge of the lint:

| file | lines | findings |
|---|---|---|
| `research/hypotheses/Test1.md` | 3,147 | **31**, all true leaks |
| `research/hypotheses/example_mean_reversion.md` | 21 | clean |
| `research/hypotheses/example_momentum.md` | 21 | clean |
| `config/hypotheses/intraday-continuation.md` | 54 | clean |

`Test1.md`'s 31: `out-of-sample` ×15, `validation` ×4, `in-sample` ×3,
`p-value` ×2, `q =` ×2, and one each of `reject`, `correction`,
`multiple testing`, `significance`, `acceptance`. That file describes its own
evaluation methodology — PBO, combinatorially symmetric cross-validation,
out-of-sample performance requirements — and if it had been used as a hypothesis
it would have handed the proposing agent the entire gate. It is 62 KB and all of
it would have gone into the prompt.

**One term was narrowed rather than kept.** A bare `threshold` produced the only
two false positives, both the same sentence: *"The paper's signal rule uses a
shifted zero threshold."* That is signal vocabulary, not the gate's bar, and a
hypothesis has to be able to say it. The pattern is now
`(?:significance|acceptance|rejection|decision)[- ]threshold`. Erring toward
rejection is right in general, but a word a hypothesis must use to state its own
rule does not belong in a banned list — and the measurement is what settled it
rather than an argument about it.

**Caveat on that false-positive rate.** `intraday-continuation.md` was written by
someone who had the banned list in front of them, so its clean result is not
evidence. The two example hypotheses are the honest sample, and two files is a
small one.

## What the byte-identity test caught

The first draft of the template's new header comment contained the literal string
`## USER`, in a sentence explaining that the assembler splits on it. `_call` does
`prompt.split("## USER", 1)` — so **the comment became the marker**, the real
heading below was never reached, and both assembled messages came out wrong: the
SYSTEM message was the HTML comment and the USER message began mid-sentence.

Nothing else would have caught this. The template still rendered, the placeholders
still filled, no exception was raised, and the campaign would have run with a
model being handed a prompt whose system message was a note to maintainers. The
test that compares the assembled messages against
`tests/fixtures/strategy-agent-task45.md` failed immediately and printed the diff.

The comment now says not to write either heading's literal text, and why.

## What did not run

**Leg 3: a real model proposing under the hypothesis.** The gateway would serve
nothing — `claude-haiku-4-5` 503 upstream, `gemini-3.6-flash` 503,
`claude-sonnet-5` and `claude-fable-5-1` 403 at the proxy. All four tried, none
served.

**No substitute model was used and the leg is not simulated.** Task 37 is the
precedent: the gateway once silently served `gemini-3.1-pro-preview` for
`claude-haiku-4-5`, and that campaign was discarded. `preflight()` exists because
of it and it did its job here.

**So the brief's last report-back item is unanswered:** whether the five
proposals are recognisably *about* the question. Legs 1 and 2 establish that the
question reaches the model's input and travels through the record to a verdict.
They cannot establish that a model reads it and proposes accordingly. Re-run
`end_to_end.py` when the gateway is up; nothing else needs to change, and the
script exits 2 rather than 0 until that leg runs.

## Leg 2, for the record

```
N 5   outcomes {'completed': 5}   unaccounted 0   tampered False
record: hypothesis_path   config/hypotheses/intraday-continuation.md
record: hypothesis_digest 38c817dc28bab487   still matches the file: True
record digest verifies    True (b05b5b22dea96221)
tier 3: CANNOT_TELL   escalate True   can_promote False
  - this campaign has no gate verdict to audit
  - the best candidate's source is absent, so leakage review was not possible
```

`CANNOT_TELL` is the correct answer at N = 5 and is the member that exists so
tier 3 does not have to pretend. The proposer for this leg is the **stub**; its
candidates are edgeless by construction and are not an attempt to answer the
question.
