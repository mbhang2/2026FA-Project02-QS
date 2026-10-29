"""The AST bridge, loaded in the strategy process as `harness_alpha`.

Nalin's `src/primitives` is a VECTORISED library: pandas `Series -> Series`
operators, an AST over them, and `compile_strategy_weights` producing a target
weight per bar for a whole frame at once. This engine is a STREAMING one:
`on_bars(ctx)` fires per bar, reads `ctx.history(interval)`, and calls
`place()`. Nothing here consumes a weight vector, so the two need a bridge,
and the bridge has three jobs it must get right.

## 1. Evaluate on a trailing window, not the whole history

Rebuilding the frame from bar 0 every bar is what makes the library look
unusable. Measured on 20,475 bars, one NQ campaign, per bar and projected to a
whole trial:

    operator                     full history      trailing window
    ts_zscore                    0.68 ms -> 14 s   0.22 ms -> 4.6 s
    a six-node nest              0.83 ms -> 17 s   0.31 ms -> 6.4 s
    ts_rank (before the rewrite)  413 ms -> 2.4 h  2.89 ms -> 59 s

`lookback` is the window, and it must cover the deepest `window`/`span`/
`periods` any node in the AST asks for. `required_lookback` reads that off the
AST so a caller does not have to guess, and `Alpha` refuses a window smaller
than the AST needs rather than silently emitting NaN forever.

## 2. Call `evaluate_node`, NEVER `compile_strategy_weights`

This is the one that bites, and it is not a defect in his code.

`compile_strategy_weights` ends with `.shift(1)`, and in a vectorised backtest
that shift is *required*: there `pnl_t = weight_t * return_t`, and `return_t`
is realised at bar t's close, so a weight chosen from data through t would be
paid on a close it already saw. Verified on his code -- `weight_t` comes back
as exactly `tanh(alpha_(t-1))`.

This engine already enforces the same one bar, structurally: `on_bars` at bar
t sees bars through t, `place()` fills after t closes, so the position earning
bar t+1's return was decided from data through t. Same single bar, different
index origin.

Take the last value of `compile_strategy_weights` inside a callback and the
two compose: a signal from t-1 placed at t, earning t+1. **Two bars of lag,
silently.** So this module calls `evaluate_node` and applies `tanh` itself.

## 3. Turn a continuous weight into whole contracts

His weights are continuous on [-1, 1]; this engine trades integer contracts.
Somebody has to choose the rounding, and the choice is not neutral: round at
the nearest integer and a weight hovering near a boundary flips the position
every bar, paying the spread each time for a signal that never changed its
mind. `Alpha.target` therefore takes an explicit `max_contracts` and a
`deadband`, and a target inside the deadband of the position already held is
reported as that position rather than as a new one.

**The deadband is a judgement, not a measurement.** 0.15 of one contract is
the default because it is the smallest band that stops the churn in the
reference AST, and it is pre-registered per campaign like anything else that
changes what trades. It is NOT tuned against a score.

Nothing in this module reads the engine, the dataset or the scorer, and
nothing in it can see the gate. It is loaded by file path under a bare name --
`import harness_alpha` -- for the same reason `harness_replay` is: importing
`src.core.engine.remote.alpha_env` would execute `src/core/engine/__init__.py`
and put the engine in the strategy's process.

See `docs/design/ast-strategies.md`.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Iterable, Sequence

# **`src.primitives` is imported LAZILY, inside `Alpha`, and that is a
# boundary decision rather than a style one.**
#
# This module is installed in every strategy process so that `import
# harness_alpha` resolves. Importing the library at module level therefore
# loaded pandas, numpy and four `src.primitives.*` modules into EVERY
# strategy's process, including the ones that have never heard of an AST --
# caught immediately by `test_the_child_loads_only_the_allowlisted_harness_
# modules`, which walks the child with `src/strategies/current.py`.
#
# Deferring it means the boundary widens only for a strategy that actually
# constructs an `Alpha`, so no allowlist entry is needed and no existing
# strategy pays for this file existing. `src.primitives` is not under
# `src.core` and pulls nothing from it -- `src/__init__.py` is empty and the
# package imports only its own siblings, which
# `test_the_bridge_does_not_import_the_engine` proves in a subprocess.


def _parser():
    from src.primitives.parser import evaluate_node, validate_ast

    return evaluate_node, validate_ast

#: Keys whose value is a lookback in bars. `ts_ema`/`ts_decay_exp` take `span`,
#: the rolling operators take `window`, the shift operators take `periods`.
LOOKBACK_KEYS = ("window", "span", "periods")

#: Fields a bar can supply. The bridge builds no others, so an AST naming
#: something else fails at construction rather than at bar 4,000.
BAR_FIELDS = ("open", "high", "low", "close", "volume")

DEFAULT_DEADBAND = 0.15


def required_lookback(node: dict, *, floor: int = 2) -> int:
    """Bars of history this AST needs before it can produce a finite value.

    The sum down each branch rather than the maximum, because the operators
    compose: `ts_zscore(ts_delta(ts_ema(close, span=10), periods=1), window=20)`
    consumes a bar for the delta and twenty for the z-score on top of the EMA's
    span, and a window sized to the largest single node alone would emit NaN
    for the first thirty bars and look like a broken signal.
    """
    if not isinstance(node, dict):
        return floor
    own = 0
    for key in LOOKBACK_KEYS:
        v = (node.get("kwargs") or {}).get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            own = max(own, int(v))
    deepest = 0
    for child in (node.get("args") or []):
        deepest = max(deepest, required_lookback(child, floor=0))
    return max(floor, own + deepest)


def _fields_used(node: dict, out: set[str]) -> set[str]:
    if isinstance(node, dict):
        if node.get("type") == "field" and node.get("name"):
            out.add(str(node["name"]))
        for child in (node.get("args") or []):
            _fields_used(child, out)
    return out


class Alpha:
    """One AST, evaluated on a trailing window, turned into a contract target.

    Stateless with respect to the engine: it holds only the trailing bars it
    was given. `update` is the whole interface a strategy needs.
    """

    def __init__(self, ast: dict, *, lookback: int | None = None,
                 max_contracts: int = 1,
                 deadband: float = DEFAULT_DEADBAND) -> None:
        _, validate_ast = _parser()
        ok, why = validate_ast(ast)
        if not ok:
            raise ValueError(f"the AST is not valid: {why}")
        unknown = _fields_used(ast, set()) - set(BAR_FIELDS)
        if unknown:
            raise ValueError(
                f"the AST reads fields a bar does not have: {sorted(unknown)}. "
                f"Available: {list(BAR_FIELDS)}")
        need = required_lookback(ast)
        if lookback is None:
            # One spare bar, so the newest row is never the first finite one.
            lookback = need + 1
        if lookback < need:
            raise ValueError(
                f"lookback={lookback} is shorter than the {need} bars this AST "
                f"needs, so it would emit NaN for every bar. Pass "
                f"lookback>={need} or use the default.")
        if max_contracts < 1:
            raise ValueError(f"max_contracts must be >= 1; got {max_contracts}")
        if not 0.0 <= deadband < 1.0:
            raise ValueError(
                f"deadband must be in [0, 1) contracts; got {deadband}. It is a "
                f"fraction of ONE contract, not of the position.")
        self.ast = ast
        self.lookback = int(lookback)
        self.max_contracts = int(max_contracts)
        self.deadband = float(deadband)
        self.required = need
        self._rows: deque[dict] = deque(maxlen=self.lookback)

    # -- the two things a strategy calls ---------------------------------- #
    def push(self, bars: Iterable[Any]) -> None:
        """Add closed bars. Only the newest `lookback` are kept."""
        for b in bars:
            self._rows.append({f: float(getattr(b, f)) for f in BAR_FIELDS})

    def weight(self) -> float | None:
        """`tanh(alpha)` on the newest bar, or None while warming up.

        None rather than 0.0: "I cannot say yet" and "flat is correct" are
        different, and a strategy that cannot tell them apart will trade the
        warm-up.
        """
        if len(self._rows) < self.required:
            return None
        import numpy as np
        import pandas as pd

        frame = pd.DataFrame(list(self._rows))
        evaluate_node, _ = _parser()
        raw = evaluate_node(self.ast, frame)
        last = float(np.asarray(raw, dtype=float)[-1])
        if last != last:                       # NaN
            return None
        # No clip: tanh's range IS (-1, 1). His `compile_strategy_weights`
        # clips after tanh, which is harmless there and redundant here.
        return float(np.tanh(last))

    def _round(self, weight: float, position: int) -> int:
        """The weight-to-contracts rule, on its own so it can be argued with.

        Two choices here, both deliberate:

        * **the deadband is measured against the position already held**, not
          against the last weight. A signal that drifts without changing the
          whole-contract target leaves the position alone, so a weight
          hovering near a boundary cannot round-trip the book every bar.
        * **half rounds away from zero**, not to even. Python's `round` is
          banker's rounding, so `round(0.5)` is 0 and `round(1.5)` is 2 -- a
          target of exactly half a contract would mean "flat" at one size and
          "long" at the next, which is indefensible as a sizing rule even
          though it is defensible as a float convention.
        """
        import math

        desired = weight * self.max_contracts
        if abs(desired - position) <= self.deadband:
            return int(position)
        return int(math.copysign(math.floor(abs(desired) + 0.5), desired))

    def target(self, position: int) -> int | None:
        """Contracts to hold, given what is held now. None while warming up."""
        w = self.weight()
        return None if w is None else self._round(w, position)

    def update(self, bars: Sequence[Any], position: int) -> int | None:
        """`push` then `target`: the one call a strategy needs per bar."""
        self.push(bars)
        return self.target(position)
