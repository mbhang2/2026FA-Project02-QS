# Fitted strategies — design, for review

**Status: DESIGN. No code exists.** Written 2026-09-21 after a discussion with
Michael about running ML, DL and RL strategies through this loop. Nothing here is
in force and nothing has been implemented.

The short version: **the scoring half of this project already works for fitted
strategies and needs no change. The honesty-about-searching half does not.** The
gate, the null, the metric, the committee and the ledger do not care how a strategy
decided to buy — only what it traded and when. What breaks is the accounting of how
much searching happened, and the guarantee that no strategy saw data it should not
have.

---

## 1. What is actually different

A strategy today is **a file**. We hash it, and the hash is the strategy.

A fitted strategy is **a file plus a number of weights**, and a training procedure
that produced them. Three consequences follow, and everything below is one of them:

1. The weights are an artifact that shaped the result, and we fingerprint every
   such artifact — but we have nowhere to put them.
2. Training is a search, and this project's entire contribution is counting
   searches.
3. Training reads data, so *which* data it read is now a correctness question
   rather than a detail.

---

## 2. The contract: a second Protocol, beside `Strategy`

`src/core/types.py` declares `Strategy` as a Protocol and `engine.validate_strategy()`
checks a loaded strategy against it before the walk starts. `TickStrategy` already
extends it for strategies that subscribe to ticks. A fitted strategy is the same
move:

```python
class FittedStrategy(Strategy, Protocol):
    """A strategy whose behaviour depends on parameters fitted to data."""

    def fit(self, data) -> None: ...
    def save_weights(self, path) -> None: ...
    def load_weights(self, path) -> None: ...
```

The engine learns one new rule, and it is the load-bearing one.

### 2a. The fit window, and why it must be structural

**`fit()` may be called only on the fit slice, and the engine refuses it
anywhere else.** Not a convention, not a review item: a call to `fit()` outside the
fit window raises, the same way `setup()` returning subscriptions raises today.

This exists because of a specific, quiet failure. `--stage validate` carries a
campaign's winner to the validation split by writing its **source** and running it:

```python
strategy_path.write_text(source)   # the winner's source, then run it
```

For a strategy that is only a file, that is complete. For a fitted strategy it is
not: if the source calls `fit()`, the validation run **retrains on the validation
split**. Nothing fails, nothing is logged, and the resulting number is flattering
and meaningless. It is the same shape as the commission bug — a value the record
claims and nothing verifies — except the damage is to the one split that exists to
be uncontaminated.

So: weights are loaded by digest at validation and holdout, and a missing digest is
an error rather than a licence to refit.

### 2b. Features must be computed on the causal path

The engine makes lookahead **impossible to express**: the walk cannot see forward,
`open_ts` and `close_ts` are both on the bar so a join cannot silently take the
wrong one, and orders are T+1 eligible.

None of that reaches feature engineering. A strategy that computes a mean over the
whole fit window and uses it as a feature has learned a relationship that will not
exist at prediction time — the single commonest way an ML backtest flatters itself,
and one our safeguards do not currently touch.

**The fix is to require features to be computed by the same code that runs live,
bar by bar, through the same interface.** If feature computation must go through
the causal path, a full-sample statistic is not discouraged, it is *unexpressible*.
That restores the guarantee rather than replacing it with a promise.

The exception, stated so it is deliberate: a transform **fitted** on the fit slice
and then **frozen** — a scaler's mean and variance, say — is legitimate and
necessary. The rule is that it is fitted once, inside the fit window, and applied
unchanged everywhere after. Fitting a parameter is allowed; constructing a feature
from data the bar could not have seen is not.

---

## 3. Weights as artifacts

Weights go beside sources, under the digest that names them:

```
runs/campaigns/<id>/sources/<strategy_digest>.py      today
runs/campaigns/<id>/weights/<strategy_digest>.<ext>   proposed
```

and the digest joins `ARTIFACT_GLOBS`, so a mid-run change invalidates the trial
exactly as a rewritten prompt or hypothesis does.

**A hash of the weights is necessary and not sufficient.** Weights trained on
train+validation hash exactly like weights trained on train alone. So the row also
records the **training recipe** — the fit slice, the seed, the library versions
already covered by `lockfile_digest`, and the training script's own digest — so a
reader can regenerate them and check rather than trust.

**The champion carries both.** Today the champion is `champion.py`. For a fitted
strategy it is the source *and* its weights, and they move together or the champion
silently becomes a differently-trained model between trials.

---

## 4. The split: subdivide `train`, leave the calendar alone

Fitted strategies need a set for early stopping that is not the set they are scored
against. The obvious move is to re-partition all 543 sessions; it is the wrong one.

| | fitting days |
|---|---|
| today | **384** |
| 4:4:2 re-split, then 6:4 inside L1 | **130** |
| subdividing `train` at ~7:3 | **284** |

A 4:4:2 re-partition cuts fitting data by 66% for the one strategy family whose
recorded failure mode is already *"consistently underfit; ~267–441 positive
examples is the binding constraint"* (`docs/datasets/nq-es-qqq.md`). Scaled down,
that is roughly 90–150 examples.

It also changes a **pre-registered, fingerprinted** calendar, making every campaign
so far incomparable with everything after, and it subdivides L2 — which nothing
fits on, since L2 evaluates campaign winners and does not train them.

**Proposed:** `train` 384 → **fit 284 / early-stop 100**. Validation 86 and holdout
73 untouched. The L1/L2/holdout flow is unchanged: campaigns search on train,
each campaign's winner gets one run on validation, and the holdout opens once, by a
human, with a recorded reason.

If a larger L2 or holdout is wanted, that is a **statistical power** decision
affecting every campaign and should be taken on its own merits — bundling it here
would leave a future reader unable to tell which motivation drove it.

---

## 5. What a trial is, and what it costs in N

This is the part that cannot be solved with engineering, only with a rule.

A strategy today is one look at the data. A fitted model looks thousands of times
while training, and the counter would still say one.

**Proposed rule: a trial is one fitted configuration.** All fitting is confined to
the fit slice. **No internal model selection may touch anything the gate later
scores** — no early stopping against validation, no hyperparameter search against
validation, no picking the best of several seeds by their score on anything but the
early-stop slice.

Two honest notes:

- **Hyperparameter search across trials is still search.** An agent that proposes
  the same architecture with a different learning rate fifty times has run fifty
  trials, and the ledger counts fifty. That is correct and it is expensive.
- **Redrawing the split does not reset the counter.** The 772 looks already spent
  were on data that overlaps any new partition, so N carries over.

---

## 6. Determinism, and labelling what is not

We assert scores are bit-identical across platforms and replay every row from its
stored fill log. Training is not naturally reproducible: GPU kernels, thread
scheduling and seeding all wobble.

**Default to deterministic CPU.** Allow a toggle, but **record it in the row**, so
`ledger_check` can report "this row used non-deterministic training and is not
replayable" rather than failing to reproduce it silently years later. Do not forbid
it; label it.

## 7. Cost

Trials run about four minutes today and a campaign about four hours. Training adds
to every trial. ML profiles get a larger `wall_clock_budget_s`, and
`scripts/supervise_campaign.sh` already survives overnight runs and restarts.

---

## 8. What is deferred: reinforcement learning

**RL is out of scope for the first implementation**, for one reason beyond
infrastructure.

RL hands the agent a **second reward function, which the agent writes**. We spent
2026-09-20 watching the search game the gate's metric by not trading; Amendment 3
is the response. An RL strategy optimises a reward of its own choosing, and if that
diverges from what the gate scores, the same failure happens one level down and
invisibly.

Brian's reference — Ng, Harada & Russell, *Policy Invariance Under Reward
Transformations* — stops being an analogy here and applies literally: potential-based
shaping is the only additive class that provably preserves the optimal policy, and
any other shaping term an agent invents can change which policy wins.

That is a design problem worth its own document, after fitted strategies work.

---

## 9. What the evidence already says about deep models

Two independent sources, before any of this is built:

- **Ours.** `docs/datasets/nq-es-qqq.md` lists *"Sequence models (GRU, 1D-CNN) at
  current sample size — consistently underfit"* among the falsified families.
- **External.** `2605.17724`, surfaced by the retrieval pipeline: LSTM and gradient
  boosting on **five-minute MNQ bars with four years of data** fail to beat the base
  rate out of sample. MNQ is the micro contract on our own underlying.

We have 384 sessions. **Start with a small fitted model and frozen hyperparameters,
not a deep sequence model.** The first implementation's job is to prove the
provenance chain — weights hashed, score reproduced, looks counted honestly — on
something cheap. If that holds, larger models are an infrastructure question rather
than a research gamble.

---

## 10. Open, for review

1. **What counts as the fit slice in a non-campaign run?** A smoke run on train has
   no campaign directory to write weights into.
2. **Does the early-stop slice need pre-registering as a calendar**, or is it a
   deterministic function of the train calendar? The latter is cheaper and keeps
   the artifact set unchanged.
3. **How is a fitted champion carried across campaigns at L2?** Weights are larger
   than sources and the champion advances on strict improvement.
4. **Should `fit()` receive the data, or a loader?** Handing it the whole fit slice
   makes full-sample statistics expressible again; handing it a bar-by-bar iterator
   does not, but is harder to write against.
