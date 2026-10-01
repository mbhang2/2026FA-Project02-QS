# Task 72 — does refining the question earn its trials?

**Pre-registered before anything ran.** Written 2026-10-01. The results section is
appended, not edited in. If a number below contradicts an expectation stated here,
the expectation was wrong.

## 1. The question, and why it cannot be answered from what we have

The refinement loop spends a campaign's worth of trials writing a better question.
Three refinements have run and the gate's p went **0.0786 → 0.7530 → 0.8924**,
which looks like refinement making things worse. It is not evidence of that:

- **n = 3.**
- **The proposer changed mid-stream.** The gateway would not serve
  `claude-haiku-4-5` at the full 167k-character prompt, so `nq-s03` ran on
  `deepseek-v4-pro` while the earlier campaign on the same seed question ran on
  haiku and reached **+0.5136**. Question and proposer moved together.
- **We have never measured the run-to-run spread of best-of-20 under a fixed
  question and a fixed proposer.** Without that number, *no* sequence of p values
  is interpretable. 0.0786 → 0.8924 might be two draws from one distribution.

That last point is the real gap, and it is worth more than the comparison.

## 2. Design — two arms, same seed, same proposer

| | arm A — refined | arm B — control |
|---|---|---|
| campaign 1 | `intraday-continuation.md` | `intraday-continuation.md` |
| campaign 2 | refined from campaign 1 | **the same file again** |
| campaign 3 | refined from campaign 2 | **the same file again** |
| proposer | `deepseek-v4-pro` | `deepseek-v4-pro` |
| N per campaign | 20 | 20 |
| profile | `nq-train` | `nq-train` |

**Arm B is the measurement; arm A is the comparison.** Three independent
best-of-20 draws from one fixed question give the spread that any claim about
refinement has to clear. It is the history-withheld control of Task 36 applied one
level up: there, the control arm withheld a campaign's own scores; here it
withholds the refinement.

**120 trials. Global N 965 → 1,085.**

## 3. Nothing here is validated, and that is deliberate

**Both arms stop after phase 1.** No validation, no L2, no holdout.

This is a train-side measurement about our own machinery, and validation is one
look per campaign — a resource that exists to judge candidates, not to audit a
process. Spending six of them on a methodology experiment would be the same
mistake as opening the holdout to check the plumbing. §4 permits reading train
results freely, which is all this needs.

A `--train-only` flag on the session driver enforces it rather than relying on
nobody typing the wrong thing.

## 4. What it can resolve, stated before it runs

**Very little, and this section exists so that is not discovered afterwards.**

Three draws per arm estimate a standard deviation with about 50% relative error.
With n = 3 against n = 3, a two-sample comparison has roughly **30% power to
detect a one-standard-deviation shift** in best-of-20 at the conventional level.

So:

- **A null result here means almost nothing.** "Refinement did not beat the
  control" at this sample size is compatible with refinement helping
  substantially.
- **A clear result is still worth having in one direction:** if arm A's three
  campaigns all fall below arm B's worst, or all above its best, that is a
  6-choose-3 ordering with probability 1/20 under exchangeability, and it is
  reportable as a signal worth a larger measurement.
- **Arm B's spread is the deliverable regardless of the comparison**, and it is
  not underpowered for that: three draws bound the noise floor well enough to say
  whether 0.0786 → 0.8924 is remarkable or ordinary.

## 5. Predictions, recorded now

1. **Arm B's three best-of-20 values will span more than 0.3 Sortino.** The two
   existing runs of this question span 0.38 (+0.1321 and +0.5136) with the
   proposer differing. If a fixed question and fixed proposer still span 0.3, then
   the `nq-s03` sequence was noise and I over-read it.
2. **Arm A will not clearly beat arm B.** Three refinements is too few for a
   question to improve much, and the refiner is deliberately blind to the gate.
3. **Neither arm's best-of-20 will clear its null maximum at q = 0.05.** Nothing
   in this project has, on this question, under this proposer.
4. **If prediction 1 holds, the headline result of Task 72 is that
   single-campaign p values are not comparable to each other** — which would make
   every per-campaign comparison in this project, including the ones I have been
   narrating to Michael, a statement about noise.

## 6. How to run it

```
.venv/bin/python scripts/research_session.py --plan config/sessions/task72-control.yaml --train-only
.venv/bin/python scripts/research_session.py --plan config/sessions/task72-refined.yaml --train-only
```

Control first, because it is the half that is worth something on its own.
