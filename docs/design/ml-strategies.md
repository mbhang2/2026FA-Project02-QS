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

### 2b. The fit phase is warmup, generalised

**Decided with Michael, 2026-09-21.** The strategy does not receive the fit window
as an array. It **accumulates it through the ordinary walk**, bar by bar, with its
actions ignored — which is what `warmup_sessions` already does today, and what every
NQ profile already runs with (`warmup_sessions: 1`). At the end of the window the
engine signals the boundary, `fit()` is called once, and the model is frozen.

This makes the data boundary **structural rather than trusted**. There is no array
handed over with an instruction to use only the first 284 rows: the strategy cannot
accumulate the early-stop or validation days because the engine does not walk them
during the accumulation phase.

Four assertions, all cheap, all in the engine:

- `fit()` is called **exactly once**, at the boundary.
- **No orders are accepted before it.** Warmup already ignores actions and counts
  them (`warmup_actions_ignored`), so this is nearly free.
- `fit()` **refuses after the window closes**, which is section 2a's rule and is
  what stops `--stage validate` retraining on the validation split.
- A sample of fit-window bars is **replayed through the live path** and its features
  compared against what `fit()` was trained on. See below.

### 2c. The fit/predict consistency check

An earlier draft of this document said features "must be computed on the causal
path", and proposed that the engine orchestrate feature computation itself so
lookahead would be unexpressible. **That was aiming at the wrong target.**

The danger is not that a strategy can see the whole fit window. Fitting a scaler on
that window and freezing it is legitimate and necessary. The danger is that
**feature computation differs between fit time and predict time**:

| feature | at fit time | live | verdict |
|---|---|---|---|
| `(x − μ) / σ`, scaler frozen at fit | frozen μ, σ | same frozen μ, σ | **legitimate** |
| volume rank among all fit-window days | rank of 284 | rank of days so far | **skew — caught** |
| next-day return | computable | does not exist | **skew — caught** |

All three "see all the data". Only the first is sound. A restriction on the handoff
cannot tell them apart; a comparison of the two computations can.

**So: replay a sample of fit-window bars through the live path and require the
features to match.** A mismatch fails the trial rather than producing a flattering
number. This is training/serving skew, which is a known production failure, turned
into a mechanical check.

A strategy may therefore compute fit-time features however it likes — vectorised,
pandas, whatever is fast — provided the per-bar path it will use live produces the
same numbers. If it implements *only* the per-bar path, the engine builds the matrix
by replaying it: slower, and nothing extra for the author to write.

### 2d. Why not have the engine own feature computation

Recorded because it was the earlier proposal and was rejected on three grounds:

- **Code safety.** It requires new engine code *in the scored path*, which is the
  riskiest place in this repository to add any. The warmup route reuses machinery
  that already exists and is already tested.
- **Difficulty.** The engine would need to know feature schemas, assemble matrices
  and handle shape variation — heavy coupling between engine and strategy.
- **Error-proneness.** An engine bug affects every strategy; a strategy bug affects
  one trial. Putting the complexity in the engine maximises the blast radius.

It bought one property — causality by construction — that section 2c obtains more
cheaply.

---

### 2e. What the trial is scored on

**Fit on the early part of train, score on the later part. Walk-forward is
deferred.**

A model fitted on train days 1–284 and scored on days 1–384 is scored in sample and
the number is meaningless. So the scored window is the post-fit stretch only.

Today that is about 100 sessions against the 384 a non-fitted strategy gets, which
is noisy and not comparable. **More data resolves this directly**, and it is the
reason to prefer the simple split: at roughly 1,500 sessions one could fit on 1,100
and score on **384** — the exact window the four existing campaigns used, keeping ML
and non-ML trials comparable. That property is worth more than it looks and
walk-forward does not have it.

**Walk-forward — refit periodically, score each out-of-fit stretch — is the standard
answer and is deferred**, for reasons that are practical rather than statistical:

- It **breaks the one-trial-one-weights-digest story** that section 3 depends on. K
  refits means K weight sets in a single row.
- K refits multiply the cost of every trial, in a campaign that is already 50 of
  them.
- **Fold-boundary leakage is the classic silent failure** of walk-forward
  implementations: it inflates results and is hard to see.
- It averages over old regimes, where scoring the most recent stretch tests what
  would actually be deployed.

It does not inflate N — one configuration refitted on a rolling window is one
strategy — so the objection is complexity, not honesty. Revisit it when there is a
reason beyond "it is standard".

---

## 3. Weights as artifacts

Weights go beside sources, under the digest that names them:

```
runs/campaigns/<id>/sources/<strategy_digest>.py      today
runs/campaigns/<id>/weights/<strategy_digest>.<ext>   proposed
```

**They are recorded in the row by digest, and deliberately NOT added to
`ARTIFACT_GLOBS`.** An earlier draft of this section said the opposite, and it was
wrong: that set is compared at load and again after the run, so anything appearing
inside it mid-run invalidates the trial — and weights are *written* during the run,
at the fit boundary. Globbing them would make every fitted trial invalidate itself,
the same failure a stray file in `config/hypotheses/` caused on `nq-orderflow-02`.

The right precedent is `fill_log_digest`: an **output**, addressed by digest in the
row, verifiable afterwards. A validation or holdout run that *loads* weights is the
other case, and there the digest recorded here is what it must match.

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

**Proposed:** `train` splits into a **fit window** and a **scored window**, with the
scored window being the last S sessions. Validation 86 and holdout 73 untouched.

    train 384  ->  fit 1-284      scored 285-384      (S = 100 today)
    train 1500 ->  fit 1-1116     scored 1117-1500    (S = 384, once data allows)

**Early stopping lives inside the fit window and is the strategy's business.** An
earlier draft of this section said `train` split into "fit 284 / early-stop 100",
which collides with section 2e: the early-stop slice and the scored window were the
same 100 days, so a model would have been selected against the very data it was then
scored on. That is the inflation this whole document exists to avoid, introduced by
a careless split.

The engine enforces one boundary only — **fit window versus scored window** — and
that is the boundary that matters, because it is the one the score depends on. A
strategy that wants to early-stop carves a slice off the **end of its own fit
window** and declares the split in its record. Selecting against fit-window data
cannot contaminate the score, so the engine has no reason to police it.

A strategy that trains for fixed epochs needs no early-stop slice at all and gets
the whole fit window.

If a larger L2 or holdout is wanted, that is a **statistical power** decision
affecting every campaign and should be taken on its own merits — bundling it here
would leave a future reader unable to tell which motivation drove it.

---

## 5. What a trial is, and what it costs in N

This is the part that cannot be solved with engineering, only with a rule.

A strategy today is one look at the data. A fitted model looks thousands of times
while training, and the counter would still say one.

**Proposed rule: a trial is one fitted configuration, one seed, no internal
selection.** All fitting is confined to the fit slice.

### Early stopping is allowed; model selection is not

The distinction is sharp and it is the whole rule:

| | |
|---|---|
| **Early stopping** — choosing *when* to stop **one** training run | **allowed.** That is what the early-stop slice is for. |
| **Model selection** — choosing *between* configurations, seeds or architectures | **not allowed inside a trial.** Each is its own trial. |

An earlier draft of this section permitted "picking the best of several seeds by
their score on the early-stop slice", which contradicts the rule above it. That
permission is withdrawn, and here is the arithmetic that withdraws it.

A trial that grid-searches twenty learning rates inside `fit()` and keeps the best
has evaluated twenty configurations. Across a campaign of fifty trials that is
**1,000 configurations, and the ledger records 50.** `max_of_n` would then be
correcting for a twentieth of the search actually performed — which is not a
conservative approximation, it is the failure the correction exists to prevent,
arriving inside a single row where nothing can see it.

The same argument applies to seeds. Best-of-five-seeds is a five-point search whose
result correlates with the score, however innocent it looks.

**This is expensive, and the expense is the honest cost of fitted strategies
here.** Ordinary ML practice grid-searches as a matter of course. In this loop every
grid point is a look at the data, so a twenty-point sweep is twenty trials, not one
well-tuned trial. An agent that wants to tune must spend N to do it.

### Where an architecture change belongs

Whether GRU → LSTM is a trial or a campaign follows from what the hypothesis
claims, the same way direction and horizon do:

- Hypothesis names the *phenomenon* ("order flow predicts returns") → architecture
  is an implementation choice, and each one is a **trial** inside the campaign.
- Hypothesis names the *architecture* ("sequential models capture structure static
  ones miss") → the architecture is the claim, and each is its own **campaign**,
  corrected by `max_of_m` at L2.

Both are counted. Only the correction differs.

### Two further notes

- **Redrawing the split does not reset the counter.** The 772 looks already spent
  were on data that overlaps any new partition, so N carries over.
- **Nothing here is enforceable by the engine**, and that should be said plainly. A
  strategy that grid-searches inside `fit()` looks identical to one that does not.
  The fit-window rule in section 2a is structural; this one is not, and it relies
  on the proposing agent's prompt, the recorded training recipe, and the economics
  auditor reading the source. That asymmetry is worth knowing rather than assuming
  away.

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

## 10. Settled — 2026-09-21, with Michael

1. **Weights on a non-campaign run go to `runs/weights/`.** A campaign trial writes
   to `runs/campaigns/<id>/weights/`, but `smoke`, `check` and `replay` runs have no
   campaign directory. `runs/sources/` and `runs/fills/` already exist for exactly
   that case, so weights sit beside them. Consistency, not a decision.
2. **The early-stop slice needs no calendar.** The engine enforces only the
   fit/scored boundary, a deterministic function of the train calendar. Early
   stopping happens inside the fit window and is declared in the strategy's record,
   so the fingerprinted artifact set is unchanged.
3. **A fitted champion carries its weights.** `champion.py` becomes source *and*
   weights, copied together on a strict improvement. They must move as a pair or
   the champion silently becomes a differently-trained model between trials -- the
   same failure as a stale `hypothesis_digest`, and just as quiet.
4. **`fit()` receives neither a slice nor a loader.** It accumulates through the
   walk (2b), and the fit/predict consistency check (2c) does the work a loader
   restriction was meant to do. The engine-owned feature pass is rejected in 2d.
5. **S = 100.** The scored window is the last 100 sessions of train, leaving 284 to
   fit on. Pre-registered rather than chosen per campaign, because a scored-window
   length picked per campaign is a tunable that shapes results.

   **Revisit when more data lands.** At roughly 1,500 sessions, S = 384 would make
   ML trials scored on the same window as the four campaigns already run, which is
   worth more than the extra fitting days. Changing S is a pre-registration
   amendment, not a config edit.

## 11. The remote boundary — found during implementation, 2026-09-22

**A fitted strategy cannot yet run through the scored path, and the reason is
structural rather than a missing line.**

`run.py` does not hand the engine the strategy object. It hands it a
`RemoteStrategy` proxy, and the real strategy lives in a **child process** —
that separation is what keeps agent-written code away from the harness, and it is
load-bearing for isolation rather than incidental. The proxy forwards the
callbacks the child understands: `load`, `stop`, `setup`, `on_bars`, `on_tick`,
`on_fill`, `on_lifecycle`.

It does not forward `fit`, `save_weights` or `load_weights`. So `hasattr(strategy,
"fit")` is False in the engine, the fitted path never engages, and a profile with
`fit_sessions` set fails with *"RemoteStrategy is not a fitted strategy"*.

Everything in sections 1–10 is right and tested; it is tested **in-process**, which
is how `Engine.for_test` runs. The gap is only between the engine and the child.

**What closing it needs**, and why it is not a five-minute change:

- Three new message kinds in `src/core/engine/remote/child.py`, and matching
  methods on `RemoteStrategy`.
- `_surface.py` regenerated, since it is generated from the surface that crosses.
- A decision about where `fit()` executes. It must be **in the child**, because
  that is where the accumulated state lives — which means the child does the
  training, under the child's CPU and memory limits, and a training run that
  exceeds them has to fail legibly rather than as a timeout.
- The weight methods take a path. The child can write it directly if the path is
  on shared filesystem, which it is; but that is a new thing the sandboxed process
  is allowed to write, and the write-path allowlist
  (`scripts/check_agent_paths.py`) has an opinion about that.

**This is the isolation boundary.** It is the mechanism that stops a strategy
reaching the scorer, the splits or the ledger, and the reason
`tests/test_boundary.py` walks it by AST and by `sys.modules`. Widening it deserves
its own review rather than being folded into a late-night session — which is why
this section is a finding and not a patch.

Until then, fitted strategies work through `Engine.for_test` and not through
`run.py`, and `config/run.yaml`'s `nq-train-fitted` profile is written but will
fail if used.

### The way through — 2026-09-22

**Correcting this section's fourth bullet first.** It says the child "can write it
directly if the path is on shared filesystem, which it is". It cannot.
`config/strategy.sb` denies `file-write*` at line 15, and the Linux path
binds `/` read-only. `scripts/check_agent_paths.py` was never the binding constraint here;
the sandbox is. An abandoned attempt at the writable bind survived as dead code in
`adapter.py` until commit `e9a3b1d`.

**The principle: do not widen the boundary for a capability the first campaign
does not need.**

The three methods are not one feature. `fit` is needed by the first fitted
campaign. `save_weights` and `load_weights` are needed only when a champion
crosses from train to validation at L2. Splitting them splits the risk, and the
half that carries all of the risk turns out to be the half we can defer.

#### Phase 1 — forward `fit` only. No new write surface at all.

- One new message kind, `fit`, with no payload. The child calls `strategy.fit()`
  (no ctx, per `FIT_ARITY`) and replies with no actions.
- `RemoteFittedStrategy(RemoteStrategy)` carries the method, so the ordinary proxy
  stays byte-for-byte what it is today and a non-fitted run gains no surface at
  all.
- **Selection is cross-checked against the code.** The child's `load` reply reports
  which of the three methods the loaded class actually defines; `start()` raises if
  that disagrees with the class `run.py` constructed. This catches the silent
  direction: a profile that omits `fit_sessions` for a class that *has* `fit()`
  would otherwise score a model whose parameters were never fitted, and it would
  look like a real result.
- The reply may carry a **weights digest** — 64 hex characters the child computes
  over its own fitted parameters. No bytes cross and nothing is written, but the
  row gains the evidence a later refit can be checked against. A child that lies
  about its own hash defeats only our determinism check; it cannot corrupt
  anything, because nothing downstream trusts the value.

That is sufficient for an entire fitted campaign: fit `[0, 284)`, score
`[284, 384)`, one run, one split, nothing persisted.

#### Phase 2 — persistence, and only when L2 needs the crossing

Weights return as bytes over the pipe that already carries child-to-parent data,
and the parent writes them. This adds no new *primitive* — the child could always
send bytes — it adds a new thing the parent does with what it receives, and that
is what needs bounding. Each control answers one specific way it could go wrong:

1. **The path is parent-chosen.** The child supplies no filename, no path
   fragment, nothing that reaches a filesystem call. `store_weights` derives the
   path from a digest the parent computes over the bytes it received.
2. **The parent never deserializes the blob.** It writes bytes and hashes bytes.
   No `pickle.loads`, no `torch.load`, no `np.load(allow_pickle=True)` — each is
   arbitrary code execution *outside* the sandbox, which would be a worse hole
   than the one being closed. Only the child deserializes, on `load_weights`,
   inside the sandbox where untrusted code was already assumed to run. Guarded by
   a source-grep test in the style of `tests/test_global_n.py`.
3. **Size cap checked before allocation.** The declared length is refused over the
   cap before the parent reads, or a child can exhaust the parent's memory.
4. **One blob per run**, mirroring the engine's refusal of a second `fit()`.
5. **Refused from a non-fitted run** as a protocol violation, so the message type
   exists only where it was declared.
6. **Atomic write**, temp then rename; and on `load_weights` the parent verifies
   the file's digest against the one recorded on the row before sending, so a
   tampered weights file cannot silently change a scored result.

#### What this does not defend against

Weights are opaque parameters, so a child could encode something other than a
model in them and read it back in a later run. That channel is inherent to
persistence and no amount of inspection closes it. What bounds it is not the
sandbox but the pre-registration: the fit window is registered (Amendment 4), the
engine refuses a second `fit()`, the scored calendar excludes the fit window, and
a blob crosses runs only where a human wired `weights_path` into a profile.
Residual, and accepted deliberately rather than overlooked.

#### How we will know the boundary still held

Tests, not assertion:

- the existing sandbox test — a child attempting `open(path, "w")` still raises
- a non-fitted remote strategy has no `save_weights`, and a weights message from
  one is refused
- the written path derives from the parent's digest and from nothing in the message
- an oversized blob is refused before allocation
- `load_weights` refuses a blob whose digest does not match the row
- the source-grep guard from control 2

#### The alternative considered: refit instead of persist

Deterministic CPU training is the default (§6), so refitting reproduces the
identical model and no weights need to cross at all. This is the *safest* option
and it is not dismissed on safety. It is dismissed because it moves the cost
rather than removing it: a validation run would have to fit on train sessions it
does not otherwise receive — `sessions_for` hands a run its own split plus warmup
— so the engine would need a fit window drawn from a different split than the
scored one. That is a change to the gate tier instead of the isolation tier, which
is not obviously the better trade. Worth revisiting if phase 2 proves awkward. For
phase 1 it is moot, because nothing crosses.

## 12. Still open

Nothing blocking implementation. Two things to decide before the **first registered
ML campaign**, neither of which affects the code:

- **Amendment 4 to the pre-registration.** This document changes what a trial means
  for a fitted strategy and adds a fit/scored boundary inside train. Both are
  pre-registration-level and belong in an amendment, not here.
- **Whether the agent may propose fitted strategies at all, or only a human.** The
  proposer prompt says nothing about fitting, and an agent that writes `fit()`
  without understanding the one-configuration rule will spend N quickly. Worth
  deciding deliberately rather than discovering.
