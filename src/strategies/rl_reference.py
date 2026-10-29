"""A one-parameter policy trained by REINFORCE. Not a candidate.

The chain `fitted_reference.py` proves, plus the one thing that file cannot:
training by acting against the harness-owned replay, with one configuration,
one seed and no selection among the steps. The weight after the pass is the
weight that trades. Nothing picks the best step.

`import harness_replay` resolves only after the child has loaded
`src/core/engine/remote/replay_env.py` by file path, which it does immediately
before `fit()`. An in-process test registers the same module under that name.
Importing the environment through `src.core.engine` would load the engine into
the strategy process.

See `docs/design/rl-strategies.md`.
"""

from __future__ import annotations

import math
import struct


class RLReference:
    """Long when the frozen weight says the last return was large enough."""

    COMMISSION = 0.01
    LEARNING_RATE = 0.05
    SEED = 0
    POSITION_CAP = 1

    def __init__(self) -> None:
        self._bars: list = []
        self.weight = 0.0
        self._fitted = False
        self._prev = None

    def setup(self, ctx) -> None:
        ctx.subscribe("1m")

    def on_bars(self, ctx) -> None:
        bar = next(iter(ctx.closed), None)
        if bar is None:
            return
        if not self._fitted:
            self._bars.append(bar)
            return
        feature = 0.0 if self._prev is None else float(bar.close - self._prev.close)
        self._prev = bar
        want_long = self.weight * feature > 0.0
        if want_long and ctx.position <= 0:
            ctx.buy(1, type="market")
        elif not want_long and ctx.position > 0:
            ctx.sell(ctx.position, type="market")

    def on_fill(self, ctx) -> None:
        return None

    def on_lifecycle(self, ctx) -> None:
        return None

    def fit(self) -> None:
        import numpy as np

        import harness_replay

        if not self._bars:
            raise RuntimeError(
                "fit() was called with nothing accumulated. The engine calls it "
                "at the edge of the fit window; reaching it empty means the "
                "window was not walked.")
        env = harness_replay.SessionReplay(
            commission=self.COMMISSION, position_cap=self.POSITION_CAP,
        )
        rng = np.random.default_rng(self.SEED)
        weight = 0.0
        for bars in _by_session(self._bars):
            chosen: list[tuple[float, float]] = []

            def policy(i, bar, position, _chosen=chosen, _bars=bars):
                feature = 0.0 if i == 0 else float(bar.close - _bars[i - 1].close)
                p = 1.0 / (1.0 + math.exp(-weight * feature))
                take = bool(rng.random() < p)
                _chosen.append((feature, (1.0 if take else 0.0) - p))
                if take and position < self.POSITION_CAP:
                    return ("market", "buy")
                if not take and position > 0:
                    return ("market", "sell")
                return None

            reward = env.run_session(bars, policy)
            grad = sum(feature * advantage for feature, advantage in chosen)
            weight += self.LEARNING_RATE * reward * grad
        self.weight = float(weight)
        self._fitted = True
        self._prev = None

    def save_weights(self) -> bytes:
        return struct.pack("<d", self.weight)

    def load_weights(self, blob: bytes) -> None:
        self.weight = struct.unpack("<d", blob)[0]
        self._fitted = True
        self._prev = None


def _by_session(bars):
    groups: list[list] = []
    current = None
    for bar in bars:
        if current is None or bar.session != current:
            groups.append([])
            current = bar.session
        groups[-1].append(bar)
    return groups
