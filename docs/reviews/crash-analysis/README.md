# Why proposals crash, and which gate should have stopped them

Measured 2026-10-05 over session-05b and session-06, after Michael asked the
right question: **we have a gate that is supposed to stop a proposal crashing
before it runs.** We have two. One of them I broke, and the other cannot see
this class of fault.

## What the crashes actually are

Every crash in session-06 is a **runtime API misuse**, not a logic error. Two
shapes account for all of them:

    26x  AttributeError: '_ClosedBars' object has ...
         BaseContext._order() got multiple values for keyword argument 'tag'

The first is a model guessing attributes on the bar view. The second is passing
`tag` both positionally and by keyword.

## The three layers, and where each failed

**1. The static check** -- `compile()`, an AST pass, and mypy against the
Strategy protocol, with bounded repair, on every proposal. It caught none of
these, and the reason is not a bug: **mypy can only check what is typed.** A
model that writes `def on_bar(self, view, book)` with no annotations gives mypy
`Any`, and `Any.anything` is valid. Both crash shapes are invisible to it, and
both touch names that begin with an underscore.

**2. Amendment 7's probe** -- one synthetic run per proposal, with repair. This
catches both shapes on the first try. It was ON for every campaign below, and it
ran on about two in five proposals:

| campaign | width | trials | probe rows | crashed |
|---|---|---|---|---|
| `nq-s05b-01` | 4 | 100 | 53 (53%) | 11 (11%) |
| `nq-s06-01` | 4 | 100 | 41 (41%) | 3 (3%) |
| `nq-s06-02` | 4 | 100 | 42 (42%) | **35 (35%)** |
| `nq-s06-03` | 4 | 36 | 15 (42%) | 4 (11%) |

**The reason is mine.** `stage_run` armed the probe on `agent`, and Amendment 12
then built the other `width - 1` agents with `agent_for(...)`, which arms
nothing. In a width-4 campaign **three of every four proposals skipped Amendment
7 entirely.** Probe rows exceed one-in-four because a probed proposal that
crashes and repairs writes more than one row.

Fixed by `_arm_probe`, applied to every agent. The test counts probe rows against
trials rather than asserting the flag, because asserting the flag is exactly what
would have passed while this was broken.

**3. The prompt and the API documents.** A model guessing at `_ClosedBars`
twenty-six times is a documentation signal as much as a model one. The strategy
is handed its view under a private class name; nothing it is shown enumerates
that surface.

## Why 3 crashes became 35

The probe gap is identical across all four campaigns, so it is not the cause of
the variance -- it is the amplifier. What changed was the question. `nq-s06-02`'s
refinement added *"it must not be carried by its extremes"*, and candidates
satisfying that are harder to write: median trades fell 1,950 -> 813. A harder
question plus no chance to repair is how 3% becomes 35%.

**This matters for reading session-06.** `nq-s06-02` returned 0 positive
candidates of 36 scored, which reads as "there is no non-tail edge in the opening
range". Part of that result is "the proposer could not write code for this
question without a probe", and the two are not separable from this campaign
alone. The honest statement is that the question is unresolved, not answered.

## What is fixed, and what is not

Fixed: the probe is armed on every agent.

**Not fixed, and deliberately left for a decision:** annotating `view` and `book`
in the strategy template so mypy sees the protocol instead of `Any`. That would
turn both crash shapes into static errors with bounded repair, which is strictly
better than catching them in a synthetic run -- but it changes what every
proposal is asked to write, and `config/prompts/strategy-agent.md` is in
`ARTIFACT_GLOBS`, so it changes the artifact fingerprint of every campaign after
it. That is a pre-registration-adjacent change and not one to make quietly.
