# AST strategies — Nalin's primitive library, used from a streaming engine

**Status: the library and the bridge exist and are tested; no campaign has run
on them, and the proposer is not told about them.** Merged 2026-10-29 from
`origin/nalin/l1-loop` by path, not by branch merge — see §5.

`src/primitives/` is Nalin's work: ~40 registered time-series operators, a
registry, and an AST evaluator with static validation.
`src/core/engine/remote/alpha_env.py` is the bridge this document is mostly
about, loaded in the strategy process as `harness_alpha`.

---

## 1. The mismatch, stated plainly

| | his library | this engine |
|---|---|---|
| shape | vectorised: pandas `Series -> Series` | streaming: `on_bars(ctx)` per bar |
| output | a target weight per bar, for a whole frame | `place()` calls, integer contracts |
| causality | a convention — `.shift(1)` applied by hand | structural — a callback cannot see the future |

Neither side is wrong. They are two normal ways to express the same idea, and
everything below is the cost of crossing between them.

## 2. The lag, which is the part that bites

`compile_strategy_weights` ends with `.shift(1)`, and **that shift is correct
and necessary in his context.** In a vectorised backtest
`pnl_t = weight_t * return_t`, and `return_t` is realised at bar *t*'s close,
so a weight chosen from data through *t* would be paid on a close it had
already seen. Verified against his code: `weight_t` comes back as exactly
`tanh(alpha_(t-1))`.

This engine supplies the same one bar structurally. `on_bars` at bar *t* sees
bars through *t*; `place()` fills after *t* closes; so the position earning bar
*t+1*'s return was decided from data through *t*. Same single bar, different
index origin:

| | position earning the return at | decided from data through |
|---|---|---|
| this engine | *t+1* | *t* |
| `compile_strategy_weights` | *t* | *t−1* |

**They do not compose into two lags — unless a bridge reads the shifted
series.** Take the last value of `compile_strategy_weights` inside a callback
at bar *t* and you get `f(data through t-1)` placed at *t*, earning *t+1*:
two bars, silently, with no error and a plausible-looking equity curve.

So the rule is narrow and absolute:

> **The bridge calls `evaluate_node` and applies `tanh` itself. It never calls
> `compile_strategy_weights`.** That function is the right entry point for
> offline vectorised evaluation and the wrong one inside a callback.

Nothing in his library needed changing for this. `test_alpha_bridge.py::
test_the_bridge_applies_one_bar_of_lag_and_not_two` asserts both halves: that
the bridge equals the unshifted series, and that it *differs* from the shifted
one.

## 3. Affordability: a trailing window, not the whole history

Rebuilding the frame from bar 0 every bar is what makes the library look
unusable. Measured on 20,475 bars — one NQ campaign — per bar and projected to
a trial:

| AST | full history | trailing window |
|---|---|---|
| `ts_zscore` | 0.68 ms → 14 s | 0.22 ms → 4.6 s |
| a six-node nest | 0.83 ms → 17 s | 0.31 ms → 6.4 s |
| `ts_rank`, **before** the rewrite in §4 | **413 ms → 2.4 h** | 2.89 ms → 59 s |

`Alpha` keeps a `deque(maxlen=lookback)` and nothing else.
`required_lookback` reads the depth off the AST and **sums down each branch**
rather than taking the maximum, because the operators compose:
`ts_zscore(ts_delta(ts_ema(close, span=10), periods=1), window=20)` needs
31 bars, not 20. A window shorter than the AST needs is refused at
construction rather than emitting NaN forever.

As a strategy actually uses it, against the 4.1 s baseline of a real
`spx-options-train` smoke run:

| AST | ms/bar | per trial | vs baseline |
|---|---|---|---|
| `ts_zscore` | 0.301 | 6.2 s | 1.5× |
| `ts_rank` | 0.188 | 3.8 s | 0.9× |
| six-node nest | 0.398 | 8.1 s | 2.0× |

## 4. Four operators were rewritten, and the originals are the test

`ts_rank`, `ts_argmax`, `ts_argmin` and `ts_decay_linear` used
`rolling().apply()` with a Python callback — once per window, in Python.
`ts_rank` cost **413 ms a bar**, which is 2.4 hours for one trial: a campaign
would have looked hung rather than slow. They are now stride-view reductions,
and `ts_rank` is **0.188 ms a bar**, about 2,200× faster and now the cheapest
of the three measured.

**A rewrite that is merely "close" is a silent change to what every strategy
using the operator computes**, so `tests/test_primitives_vectorised.py` keeps
the original callbacks verbatim as the reference and compares element for
element — 85 cases across ties, constant series, scattered NaNs, all-NaN
windows, windows longer than the series, and windows of 1.

That test immediately earned its keep. It caught a real divergence and
revealed a fact about his originals: `rolling(window=w).apply()` defaults
`min_periods` to `w`, and **pandas counts non-NaN observations**, so a window
containing any NaN returns NaN *without the callback ever running*. The
`if np.isnan(x).all()` branch in his `ts_argmax` and `ts_argmin` is therefore
dead code, and a rewrite that faithfully reproduced that branch — as the first
one did — diverges wherever a window holds a single NaN. The rule is now
implemented once, centrally, in `_rolling_reduce`.

## 5. Why this was merged by path and not by branch

`origin/nalin/l1-loop` forked from `main` on 2026-09-08 and its diff is
**64,217 lines across 804 files, of which the AST library is 961 lines in 5
files.** The rest is a seven-week-old parallel copy of the shared codebase —
his `docs/design/architecture.md` carries 2 commits against this branch's 81,
and likewise for `src/core/engine/remote/adapter.py`, `tests/test_accounting.py`
and `tests/test_doc_citations.py`. A branch merge would have been 98.5% noise
and would have risked regressing files that have moved a long way since.

So `src/primitives/` and `tests/test_primitives.py` were taken at his content,
and nothing else. His 18 tests pass unmodified.

## 6. What is NOT built, and must not be assumed

- **No strategy uses this.** There is no example in `tests/examples/` and
  `docs/agent/strategy-api.md` does not mention `harness_alpha`, so the
  proposer cannot reach for it. Wiring it into the prompt is an artifact
  change and a separate decision.
- **The deadband is a judgement, not a measurement.** `0.15` of one contract
  is the smallest band that stops the churn in the reference AST. It changes
  what trades, so it is pre-registered per campaign — and it must never be
  tuned against a score.
- **`max_contracts` is not position sizing.** It scales a weight in [−1, 1] to
  whole contracts and nothing more. No volatility targeting, no risk budget.
- **Nothing here reads the engine, the dataset or the scorer.** The bridge is
  loaded by file path under a bare name for the same reason `harness_replay`
  is: importing `src.core.engine.remote.alpha_env` as a package would execute
  `src/core/engine/__init__.py` and put the engine in the strategy's process.
  `test_the_bridge_does_not_import_the_engine` runs a subprocess to prove it.

## 7. The alternative that may be better

A bridge adapts around the mismatch; a **compiler** would remove it. His
`registry` plus `validate_ast` are already a proposal grammar: the agent emits
an AST, a codegen step turns it into a streaming `on_bars` with incremental
accumulators, and pandas never runs at trial time. That constrains the search
space to statically-validatable expressions, which is worth something to the
gate on its own, and it makes §3 and §4 moot.

It is more work than a bridge and it is not built. The bridge is what exists.
