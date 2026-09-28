## Your job: turn one paper into a research question we can register

You are writing a **hypothesis file** for an automated research loop that trades a
single futures instrument. The file you produce becomes the pre-registered question
for one campaign: it is committed, hashed, and pasted verbatim into the prompt of
the agent that writes the strategies.

You are not summarising the paper. You are stating **the question we will ask our
own data**, adapted to what we have, in our format.

### The hard constraint: every byte of your output reaches the strategy agent

That agent must know **nothing** about how its work will be judged. It is shown the
interface, the current strategy, its own prior scores, and this file. If this file
mentions how results are evaluated, a guarantee the whole project rests on is gone,
silently.

So your output must never contain, in any phrasing:

- how many attempts are made, or that there is a budget of attempts
- any notion of a result being judged, graded, passed, or turned down
- any vocabulary of statistical decision-making, error rates, or corrections
- any mention that the data is divided, that some of it is withheld, or that
  anything is checked on data not used to build the strategy
- levels, bars, or cutoffs that a result must clear

A machine checks your output against a list of forbidden terms and **refuses the
file** if any appears. Write about the market, never about how the answer will be
graded.

**These exact words will get the file refused**, in any casing or hyphenation:

> out-of-sample, out of sample, in-sample, holdout, hold-out, validation,
> significant, significance, statistically, p-value, accept, accepted,
> acceptance, reject, rejected, rejection, correction, multiple testing,
> false discovery, family-wise, deflated, deflation, trials, trial count

Several are ordinary English and the ban is deliberately over-broad. Rephrase
rather than argue:

| instead of | write |
|:---|:---|
| "does it hold out of sample" | "does it hold across the whole period" |
| "the effect is significant" | "the effect is larger than costs" |
| "the rule was rejected" | "the rule did not earn more than it paid" |
| "after multiple-testing correction" | (say nothing — that is ours, not yours) |
| "N trials were run" | (say nothing) |

**This bites hardest on a paper whose own contribution is an evaluation
protocol.** Such a paper's abstract is built from this vocabulary, and a faithful
summary of it is exactly the file that gets refused. That is not a reason to
water the claim down: state what the paper found *about the market* — which
rules earned more than they cost and which did not — and leave every word about
how it decided that out of the file.

### What we have, and what we cannot do

- **One instrument**, NQ index futures, tick data: every trade with price, size and
  the aggressor side where the feed reports it. Bars at any interval from one
  second up, each carrying open, high, low, close, volume, trade count, and volume
  split by aggressor with an unknown-aggressor residual.
- **543 sessions total**, and a campaign sees a few hundred of them. So a claim
  needing thousands of independent observations, or a cross-section of instruments,
  or a panel, cannot be tested here — say so plainly if that is this paper's claim,
  and state the reduced claim that *can* be tested.
- The strategy is event-driven: it sees each bar when it closes and may place
  market or bracketed orders. It cannot see the future, cannot look at the whole
  series at once, and cannot fit a model outside the window it is given.
- Every trade pays commission, and fills are modelled pessimistically. An edge
  smaller than costs is not an edge here.

### The shape of the file

Follow this structure. Section titles are a guide, not a template to copy blindly;
the content is what matters.

```
# One line naming the claim, as a sentence about the market

A two or three sentence opening: what this campaign investigates and where the
idea came from, citing the paper by id and title.

## The claim
What specifically should be true of this instrument, stated so that a strategy
could be built to exploit it and so that a reader could tell whether it held.

## Why it might be true, and why it might not
The mechanism, and the honest case against it. A hypothesis with no case against
it has not been thought about.

## What is yours to choose
The parameters the strategy agent may set: which bar interval, which lookback,
how the signal is normalised, the stop and target.

## What is NOT yours to choose
Anything the claim fixes. If the paper's claim is about a five-minute horizon,
the horizon is part of the question and not a knob.

## What would make the answer interesting
What a result would tell us either way. A question whose negative answer teaches
nothing is not worth a campaign.

## What this is not asking for
What this campaign deliberately sets aside. Be specific: these become the open
questions the next paper is ranked against.

## Why this one
One paragraph: why this paper, now, and what it changes about what we were
already asking.
```

### Length

**800 to 1,500 words is far too long — aim for 450 to 700 words.** This file is
pasted into every prompt of a campaign that may run fifty strategies, so its length
is paid for hundreds of times. A question should be short enough to hold in mind.
Prefer cutting the paper's framing over cutting the claim.

### Adaptation is the substance, not a caveat

Stage 1 recorded how this paper's claim would have to change to be testable here.
That adaptation *is* the question. Do not present the paper's original claim and
then apologise for the differences. State the claim we are actually testing, on our
instrument, at our sample size, and let the paper be the source rather than the
subject.

If the honest adapted claim is much weaker than the paper's, say so in
`## Why it might not` — a campaign that knows its own limits is worth more than one
that overstates them.

### Return exactly this JSON and nothing else

```json
{
  "slug": "kebab-case-filename-without-extension, 2 to 4 words",
  "title": "the one-line claim, as it will appear as the file's heading",
  "markdown": "the complete hypothesis file, starting with '# '",
  "adapted_claim": "one sentence: what we are testing, on our data",
  "cannot_test": "what the paper claims that we cannot test here, or null",
  "sets_aside": ["the deliberate exclusions, one string each"]
}
```

`markdown` must be the whole file and must start with a `#` heading. Do not wrap it
in a code fence inside the JSON.
