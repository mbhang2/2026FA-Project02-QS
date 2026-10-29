# Reinforcement-learning strategies — design, for review

**Status: the replay and a reference policy exist; nothing here is in force
for a scored campaign.** Written 2026-10-23. It is the document
`docs/design/ml-strategies.md` section 8 deferred ("a design problem worth its
own document, after fitted strategies work"). Fitted strategies now work end to
end through the remote boundary, so the precondition holds.

`src/core/engine/remote/replay_env.py` is the environment in section 2, loaded
in the child as `harness_replay` immediately before `fit()`.
`src/strategies/rl_reference.py` trains one parameter against it. The
pre-registration amendment in section 7 has not been adopted, so no campaign
may be run on this yet.

Market making is a separate question with a separate blocker — the data — and
lives in `docs/design/market-making.md`. The two compose: an RL market maker needs
both documents, and neither one alone is enough.

The short version: **an RL policy is a fitted strategy whose `fit()` happens to
train by interacting with a simulator. Everything about the fit window, weights,
determinism and N in `docs/design/ml-strategies.md` carries over unchanged. Two
things are new and both are about who owns the simulator: the fill semantics the
policy trains against, and the reward.**

---

## 1. What is actually different from a fitted strategy

A supervised fitted strategy reads the bars it accumulated during the fit window,
computes labels from them, and solves for weights. Nothing it does during training
involves placing an order.

An RL policy learns by acting. During training it places orders, observes fills,
and is rewarded. That means `fit()` must contain a **simulator**: something that
decides whether a hypothetical order on a fit-window bar would have filled, at what
price, and what the episode's reward was.

`fit()` runs in the strategy's child process (`src/core/engine/remote/adapter.py`,
`RemoteFittedStrategy`). The engine, the fill models and the scorer are not loaded
there — `CHILD_MODULE_ALLOWLIST` permits `src`, `src.core`, `src.core.types` and
`src.strategies` and nothing else. So today a policy that wants to train has to
**write its own simulator**, and that is the whole problem:

| what the policy writes | what it would then be optimising | the failure, one level down |
|---|---|---|
| its own fill rule | fills it chose | the `favorable` / `unfavorable` lesson from `docs/design/simulator-interface.md`: a policy trained under optimistic fills learns an edge that lives in the optimism |
| its own reward | a number it controls | the Amendment 3 reward hack, invisibly: `nq-mvp-01` climbed the score by trading 3 times in 384 sessions; an inner reward with no idle charge teaches the same thing inside `fit()` and the ledger never sees it |

Both are the principle the scorer's import guard already encodes — *if a strategy
can compute its own objective, it can optimise a number it controls* — applied to
the training loop instead of the scored walk.

---

## 2. The proposal: a harness-owned replay environment in the child

**A small, pure, harness-owned module that the child may load, which replays the
fit-window bars the strategy accumulated, applies the scored fill semantics, and
computes the per-session reward the metric consumes.** The policy calls it from
`fit()`. It does not choose fills and it does not compute reward.

Proposed location: a standalone module loaded by file path inside the child (as
`protocol` and `_surface` already are, under `_remote_` names), so that importing it
does not drag `src/core/engine/__init__.py` — and with it the engine — into the
child. Task 24 and Task 25 both found the engine leaking into the child through an
`__init__` re-export; this must not be the third time.

### 2a. What it may contain, and what it must not

| allowed | forbidden |
|---|---|
| bars the strategy already holds, passed in by the strategy | any data loader, path, or dataset handle |
| the order types and validation rules from `src/core/types.py` | `src/core/engine/engine.py`, `src/core/engine/context.py` |
| fill rules equivalent to `UnfavorableFill` in `src/core/engine/fills.py` | the scorer, `src/core/scoring/metrics.py`, the null, the correction |
| the commission from the run profile, passed in at `fit()` | the Sortino score, any p-value, any threshold |
| the per-session idle charge (see 2c) | anything about validation, holdout, or the gate |

The environment computes **per-session returns**, not the score. The score is a
Sortino over the whole vector and stays behind the process boundary.

### 2b. Fill equivalence, at bar resolution, stated rather than assumed

The engine resolves resting orders tick by tick. The environment would see bars,
because that is what a fitted strategy accumulates. At bar resolution:

- **A resting buy limit at `p` fills if and only if the bar's low is strictly below
  `p`** (sell: high strictly above). This is exact for *whether* a limit that rested
  the whole bar fills under `unfavorable`, because "some trade strictly through `p`"
  is the same statement as "the low is strictly below `p`". It fills at `p`, as the
  engine does.
- **A market order fills at the next bar's open**, which is the first trade after
  the decision — the engine's T+1 rule, at bar granularity.
- **Brackets and stops are not exact.** When both a stop and a target sit inside one
  bar's range, a bar cannot say which traded first. This is the intra-bar ordering
  problem `docs/datasets/nq-es-qqq.md` names under the path envelope. The
  environment must resolve it **pessimistically** — the stop first — and say so in
  its docstring.

Per the design principle *verify cheap representations against expensive ones on a
sample*: before the environment is used in a scored campaign, replay a window of
fit-window sessions both ways — environment and engine, same orders — and assert
agreement on fills for limits and market orders, and report the disagreement rate
for brackets. State the replication count and the disagreement rate the check can
resolve before it runs.

### 2c. The reward is the series the metric consumes

**Reward per episode = that session's return as `session_returns` in
`src/core/scoring/metrics.py` builds it**: realised P&L net of commission,
attributed to the session the closing fill happened in, plus the idle charge —
`-IDLE_CHARGE × peak position × (1 − time-weighted exposure)`. One episode is one
session, because a session is the unit the score is computed over and the unit the
null resamples.

Two details of that function do not decompose cleanly into episodes, and the
environment has to make a choice about each rather than inherit one by accident:

- **Peak position is measured over the whole run**, not per session. The
  environment should fix it to the policy's declared position cap, so the reward
  for a session does not depend on what the policy did in a different one.
- **A position carried across a session boundary** books its P&L in the session it
  closes. Episodes that end flat avoid the question; a policy that holds overnight
  needs the environment to carry state across episodes, as the scored walk does.

Amendment 6's eligibility floor — a candidate exposed in under 70% of scored
sessions is ineligible — is not a reward and should not be shaped into one. The
idle charge already pushes in that direction; the floor is applied by the harness
after the walk, exactly as for any other strategy.

Why not something else:

- **Not the Sortino score.** It is not additive over sessions, so it is not an MDP
  reward, and exposing it is exposing the objective.
- **Not raw P&L without the idle charge.** On data with no edge the risk-neutral
  optimum of raw P&L is "never trade" — Amendment 3, verbatim. The idle charge is
  what reversed that gradient for the outer search; dropping it inside `fit()`
  reintroduces the hack where nothing records it.
- **Not an invented shaping term.** Ng, Harada & Russell (1999): only
  potential-based shaping is guaranteed to preserve the optimal policy, and it
  telescopes to a constant over a fixed-endpoint episode. Any other additive term the
  proposing agent writes can change which policy wins. The pre-registration's
  Amendment 3 reasoning ("Why not a penalty term on the score") applies here
  unchanged.

**Risk-neutral reward against a Sortino gate is a known mismatch, accepted on
purpose.** The policy maximises expected session return; the gate scores the
downside-adjusted ratio. The alternative — letting the policy add a downside term —
is a hyperparameter that changes the winner, so it becomes part of the trial's
configuration (section 4), not a free choice inside `fit()`.

### 2d. What this cannot enforce

The environment makes the honest path the easy one. It does not make the dishonest
one impossible. Strategy code runs in the child; a policy can ignore the
environment, write its own fill rule, and train against that. **The engine cannot
see this**, which is the same asymmetry `docs/design/ml-strategies.md` section 5
states for grid search inside `fit()`. What bounds it:

- the economics auditor reads the source;
- the recorded training recipe (section 4) names the environment and its digest;
- the scored walk still uses the real engine and the real `unfavorable` model, so a
  policy trained against optimistic fills is *scored* honestly — it just wastes a
  trial learning the wrong thing.

The third point is why this is a design for an efficient search, not a hole in the
gate. The gate never trusts anything computed in `fit()`.

---

## 3. What does not change

All of `docs/design/ml-strategies.md` applies as written. In particular:

- **The fit window is the training data.** 284 train sessions under
  `nq-train-fitted` in `config/run.yaml`; scored on the last 100. Training
  episodes are drawn only from bars the strategy accumulated in that window.
- **`fit()` is called once and the policy is frozen.** No learning during the scored
  walk, on validation, or on holdout. An online-updating policy is causal and would
  be scoreable in principle, but its weights at the end of the walk are not the
  weights the row's digest names, and a validation run that keeps learning is
  learning on validation. Out of scope for the first implementation.
- **Weights are an artifact.** Bytes cross the pipe, the parent writes and digests
  them; the champion is source plus weights, moved as a pair.
- **Deterministic CPU by default**, non-determinism labelled in the row.

---

## 4. What a trial is, and what it costs in N

The rule in `docs/design/ml-strategies.md` section 5 — **one fitted configuration,
one seed, no internal selection** — applies, with RL's configuration spelled out
because RL has far more knobs:

| part of the configuration | inside one trial | as a search |
|---|---|---|
| algorithm, network, observation set, action space | fixed | each variant is a trial |
| reward modifications (risk term, inventory penalty, shaping) | fixed, declared | each variant is a trial |
| learning rate, discount, entropy coefficient, episodes | fixed, declared | each variant is a trial |
| seed | one | best-of-k seeds is a k-point search |
| checkpoint selection | early stopping on a declared slice at the **end of the fit window** only | picking the best checkpoint by fit-window return across a sweep is model selection |

**This is the part that matters most for RL, and it is not hypothetical.** A
typical RL market-making research programme iterates on reward design — inventory
penalty coefficients, potential shaping, Lagrangian constraints, risk-sensitive
objectives — and evaluates each variant on the same out-of-sample weeks. Every one
of those variants is a look at the data. Run through this loop, each is a ledger
row and `max_of_n` corrects for all of them; run outside it, the best variant's
out-of-sample result carries no correction at all.

The row records the training recipe: environment digest, algorithm, every
hyperparameter, seed, episode count, the early-stop slice if any, and
`lockfile_digest` for library versions.

---

## 5. Constraints that bound the first implementation

These are facts about the repository today, not preferences.

- **No deep-RL libraries.** The locked environment has numpy and pandas; no torch,
  no RL framework. `AGENTS.md` requires user approval for a new dependency. A first
  implementation is a numpy policy — tabular, linear, or a small MLP trained by hand.
  Adding torch is a dependency decision, and it also raises the determinism question
  in section 6 of `docs/design/ml-strategies.md`.
- **No GPU.** Scored campaigns run on the Linux runner under bubblewrap with
  `RLIMIT_AS` and `RLIMIT_CPU`. Training happens inside those limits, within the
  profile's `wall_clock_budget_s` (7,200 s for `nq-train-fitted`). A training run
  that exceeds them must fail legibly, not as a timeout.
- **284 episodes.** One session per episode and 284 fit sessions. Sequence models
  are already listed as falsified at this sample size in `docs/datasets/nq-es-qqq.md`.
  Resampling sessions into more episodes does not create more independent
  information. Start small; the first implementation's job is to prove the
  provenance chain, as `src/strategies/fitted_reference.py` did for supervised
  models.

---

## 6. Build order, if adopted

1. **Amendment to `docs/design/pre-registration.md`**, by a human, before any code
   that depends on it: what a trial is for an RL strategy (section 4), and that the
   reward is fixed by the harness (section 2c). Draft text in section 7.
2. **The environment module**, pure, loaded by file path in the child, with a test
   that the child's loaded-module set is unchanged apart from it.
3. **The equivalence check** in section 2b, on synthetic first
   (`src/core/data/synthetic.py`), then on a sample of NQ fit-window sessions.
4. **A reference RL strategy** under `src/strategies/`, numpy only, deterministic,
   that trains through the environment — the RL counterpart of
   `src/strategies/fitted_reference.py`.
5. **A synthetic positive control**, in the style of `scripts/positive_control.py`:
   a synthetic dataset with a planted edge the policy can learn, to show the gate
   can accept an RL strategy that has one before anyone believes a rejection.
6. Only then a registered campaign.

---

## 7. Draft amendment text — not in force

> **Amendment N — what a trial is when the strategy learns by acting.**
> A strategy whose `fit()` trains by interacting with a simulator is a fitted
> strategy under Amendment 4, and everything in Amendment 4 applies. In addition:
> (a) its training reward is the per-session return series the metric consumes,
> including the idle charge, computed by the harness-owned environment and not by
> the strategy; (b) its algorithm, reward modifications, hyperparameters, seed and
> episode count are its configuration, fixed within a trial and recorded in the row,
> and each distinct configuration is a separate trial; (c) its policy is frozen after
> `fit()` and does not update on the scored window, validation or holdout.

---

## 8. Open, for Brian and Michael

- **Whether the idle charge's value may appear in the child at all.** Including it
  is what makes the inner reward match the scored series. It is also part of the
  metric, and `docs/design/loop.md` says the proposing agent is shown nothing about
  the gate. The charge is in the public pre-registration, and a strategy may already
  know its own P&L, so it is arguably not the gate. That is a judgement for the
  people who wrote that rule, not for this document.
- **Whether the proposing agent may write RL strategies, or only a human.** The same
  question `docs/design/ml-strategies.md` section 12 left open for fitted strategies,
  with a larger configuration space and so a faster way to spend N.
- **Whether a risk-sensitive reward belongs in the harness.** If the team decides a
  downside term is worth having, it should be one fixed, harness-owned variant
  chosen before any campaign, not something each trial chooses.
