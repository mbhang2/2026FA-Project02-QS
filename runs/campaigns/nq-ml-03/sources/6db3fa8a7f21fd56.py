"""Mean-reversion on distance from Volume Point of Control.

A simpler fitted strategy that isolates a single Auction Market Theory feature:
the distance of current price from the session's volume point of control (VPOC).
The hypothesis is that price tends to revert toward the VPOC when it strays too
far. The threshold for "too far" is fitted from the fit window as a high
quantile of absolute distances observed there.

This contrasts with the previous multivariate ridge regression, which may have
overfit noise across five features. By focusing on one robust feature and a
single fitted parameter, we hope to reduce variance and commission drag while
still capturing the mean-reversion signal.

The strategy trades only when the absolute distance exceeds the fitted
threshold, entering long when price is far below VPOC and short when far above.
A fixed bracket (stop and target offsets) is used for risk management. Decision
cadence and bracket size are hyperparameters; this trial uses a 2-minute
decision interval and 8-point bracket, matching the best prior trial for
comparability.
"""

from __future__ import annotations

import numpy as np

# -- hyperparameters --------------------------------------------------------- #
INTERVAL = "1s"          # 1-second bars for profile construction
DECIDE_EVERY = 120       # decision every 2 minutes
WARMUP_BARS = 600        # per session, before profile is meaningful
QUANTILE = 0.90          # trade when |dist| exceeds this quantile of fit window
STOP_POINTS = 8.0
TARGET_POINTS = 8.0
SIZE = 1
TICK_SIZE = 0.25


class Strategy:
    def __init__(self) -> None:
        from src.core.features.volume_profile import SessionProfile

        self.profile = SessionProfile(TICK_SIZE)
        self.session: int | None = None
        self.bars_in_session = 0

        # accumulate distances during fit window
        self.dist_samples: list[float] = []
        self.threshold: float | None = None   # set by fit(), frozen after

    # -- the walk ------------------------------------------------------------ #
    def setup(self, ctx) -> None:
        ctx.subscribe(INTERVAL)

    def on_bars(self, ctx) -> None:
        history = ctx.history(INTERVAL)
        if not history:
            return
        bar = history[-1]

        if bar.session != self.session:
            self._new_session(bar)

        self.profile.add_bar(bar.low, bar.high, bar.volume)
        self.bars_in_session += 1

        # decision only every DECIDE_EVERY bars
        if self.bars_in_session % DECIDE_EVERY:
            return

        dist = self.profile.distance_from_vpoc(bar.close)
        if dist is None or self.bars_in_session < WARMUP_BARS:
            return

        # dist is in ticks; convert to points for consistent threshold
        dist_points = dist / 100.0

        if self.threshold is None:
            # fit window: collect sample
            self.dist_samples.append(dist_points)
            return

        # trading phase
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        if dist_points < -self.threshold:
            ctx.buy(size=SIZE, stop_offset=STOP_POINTS,
                    target_offset=TARGET_POINTS, tag="vpoc-meanrev-long")
        elif dist_points > self.threshold:
            ctx.sell(size=SIZE, stop_offset=STOP_POINTS,
                     target_offset=TARGET_POINTS, tag="vpoc-meanrev-short")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    # -- session boundary ---------------------------------------------------- #
    def _new_session(self, bar) -> None:
        self.profile.reset()
        self.session = bar.session
        self.bars_in_session = 0

    # -- fitting ------------------------------------------------------------- #
    def fit(self) -> None:
        """Compute the distance threshold as a high quantile of fit-window samples."""
        if len(self.dist_samples) < 1000:
            raise RuntimeError(
                f"fit() reached with only {len(self.dist_samples)} distance samples, "
                "far below what the fit window should provide. Check WARMUP_BARS "
                "and DECIDE_EVERY; do not fit a threshold to this.")
        abs_samples = np.abs(np.asarray(self.dist_samples))
        self.threshold = float(np.quantile(abs_samples, QUANTILE))

    # -- serialization ------------------------------------------------------- #
    def save_weights(self) -> bytes:
        if self.threshold is None:
            raise RuntimeError("save_weights() before fit()")
        return np.array([self.threshold], dtype=np.float64).tobytes()

    def load_weights(self, blob: bytes) -> None:
        w = np.frombuffer(blob, dtype=np.float64)
        if w.size != 1:
            raise ValueError(f"expected 1 weight, got {w.size}")
        self.threshold = float(w[0])
