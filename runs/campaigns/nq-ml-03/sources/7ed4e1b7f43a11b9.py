"""Auction Market Theory features, supervised. Polynomial feature expansion.

Builds on the best prior (#0) by expanding the five AMT features with squares
and selected interactions, giving the ridge regression a chance to capture
non-linear relationships among the primitives. All other hyperparameters are
unchanged from #0: decision every 2 minutes, forward horizon of 3 decisions
(6 minutes), ridge lambda 0.5, trade fraction 0.10, and symmetric 8-point
bracket. The expanded feature vector has 15 dimensions (5 original + 5 squares
+ 5 interactions), regularised by the same ridge penalty.

This is a legitimate trial under Amendment 4: a different feature
representation is a separate trial.
"""

from __future__ import annotations

from collections import deque

import numpy as np

# -- the registered feature set ---------------------------------------------- #
FEATURES = ("dist_vpoc", "va_pos", "at_lvn", "cvd_div", "tape_velocity")
N_FEATURES = 15          # 5 original + 5 squares + 5 interactions

# -- hyperparameters. Each value tried is its own trial under Amendment 4. ---- #
INTERVAL = "1s"          # registered by docs/reviews/task67; ticks are over budget
DECIDE_EVERY = 120       # decision every 2 minutes (same as #0)
HORIZON_STEPS = 3        # label lookahead, in decisions -> 6 minutes (same as #0)
WARMUP_BARS = 600        # per session, before the profile means anything
RIDGE_LAMBDA = 0.5       # same as #0, but now applied to 15 features
TRADE_FRACTION = 0.10    # act on the top 10% of decisions by |prediction| (same as #0)
RESERVOIR = 8_000        # training rows kept, uniformly at random, to set the edge
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

        # session state for the features
        self.cum_delta = 0
        self.session_open: float | None = None
        self.tick_count_sum = 0

        # the label buffer: one entry per DECISION, not per bar
        self.pending: deque = deque(maxlen=HORIZON_STEPS + 1)

        # the normal equations, which is all training data ever becomes
        self.XtX = np.zeros((N_FEATURES + 1, N_FEATURES + 1))
        self.Xty = np.zeros(N_FEATURES + 1)
        self.rows_used = 0
        self.rows_dropped = 0

        # Reservoir sample of training rows to set the trade threshold.
        self.reservoir: list = []
        self.rows_seen = 0
        self._rng = np.random.default_rng(64_001)

        self.weights: np.ndarray | None = None   # set by fit(), frozen after
        self.edge: float | None = None           # set by fit(), frozen after

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
        self.cum_delta += bar.delta
        self.tick_count_sum += bar.tick_count
        self.bars_in_session += 1

        # Compute features only at decision cadence.
        if self.bars_in_session % DECIDE_EVERY:
            return

        x = self._features(bar)
        self._retire_labels(bar)
        if x is not None:
            self.pending.append((x, bar.close, bar.session))

        if self.weights is None or self.edge is None:
            return                            # not fitted yet: fit window
        if not ctx.actionable or x is None:
            return
        if ctx.position or ctx.working_order_ids:
            return

        predicted = float(np.dot(self.weights, np.append(x, 1.0)))
        edge = self.edge
        if predicted > edge:
            ctx.buy(size=SIZE, stop_offset=STOP_POINTS,
                    target_offset=TARGET_POINTS, tag="amt-long")
        elif predicted < -edge:
            ctx.sell(size=SIZE, stop_offset=STOP_POINTS,
                     target_offset=TARGET_POINTS, tag="amt-short")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    # -- features ------------------------------------------------------------ #
    def _new_session(self, bar) -> None:
        self.profile.reset()
        self.session = bar.session
        self.bars_in_session = 0
        self.cum_delta = 0
        self.tick_count_sum = 0
        self.session_open = bar.open

    def _features(self, bar):
        """The five AMT primitives, expanded to 15 features, or `None`."""
        if self.bars_in_session < WARMUP_BARS or self.profile.is_empty:
            return None
        band = self.profile.value_area()
        if band is None:
            return None

        dist = self.profile.distance_from_vpoc(bar.close)
        if dist is None:
            return None
        va_pos = self.profile.value_area_position(bar.close)

        nodes = self.profile.low_volume_nodes()
        at_lvn = 1.0 if any(abs(bar.close - n) <= TICK_SIZE for n in nodes) else 0.0

        span = max(self.profile.total_volume, 1)
        flow = self.cum_delta / span
        move = (bar.close - self.session_open) / max(abs(self.session_open), 1.0)
        cvd_div = flow - move * 100.0

        mean_ticks = self.tick_count_sum / max(self.bars_in_session, 1)
        velocity = (bar.tick_count / mean_ticks - 1.0) if mean_ticks > 0 else 0.0

        # Base features (normalised as in #0)
        f0 = dist / 100.0
        f1 = float(va_pos)
        f2 = at_lvn
        f3 = cvd_div
        f4 = velocity

        # Polynomial expansion: squares and selected interactions
        return np.array([
            f0, f1, f2, f3, f4,          # original
            f0*f0, f1*f1, f2*f2, f3*f3, f4*f4,   # squares
            f0*f1, f0*f3, f1*f3, f2*f3, f3*f4    # interactions
        ])

    # -- training, without ever holding a training set ----------------------- #
    def _retire_labels(self, bar) -> None:
        """Turn the oldest pending row into a label, if its future has arrived."""
        if self.weights is not None or len(self.pending) <= HORIZON_STEPS:
            return
        x, price, session = self.pending[0]
        if session != bar.session:
            self.pending.popleft()
            self.rows_dropped += 1
            return
        y = bar.close - price
        row = np.append(x, 1.0)
        self.XtX += np.outer(row, row)
        self.Xty += row * y
        self.rows_used += 1
        self.pending.popleft()

        self.rows_seen += 1
        if len(self.reservoir) < RESERVOIR:
            self.reservoir.append(row)
        else:
            j = int(self._rng.integers(0, self.rows_seen))
            if j < RESERVOIR:
                self.reservoir[j] = row

    def fit(self) -> None:
        """Ridge, solved from the accumulated normal equations. Called once."""
        if self.rows_used < 1000:
            raise RuntimeError(
                f"fit() reached with {self.rows_used} usable rows, which is far "
                f"below what a {HORIZON_STEPS}-step horizon over the fit window "
                f"implies. "
                f"{self.rows_dropped} were dropped at session boundaries. Either "
                "the window is too short or the features are returning None "
                "everywhere -- do not fit a model to this.")
        ridge = RIDGE_LAMBDA * np.eye(N_FEATURES + 1)
        ridge[-1, -1] = 0.0                      # never penalise the intercept
        self.weights = np.linalg.solve(self.XtX + ridge, self.Xty)

        preds = np.abs(np.asarray(self.reservoir) @ self.weights)
        self.edge = float(np.quantile(preds, 1.0 - TRADE_FRACTION))

    # -- the parameters, as bytes; the harness owns the filesystem ----------- #
    def save_weights(self) -> bytes:
        if self.weights is None or self.edge is None:
            raise RuntimeError("save_weights() before fit()")
        return np.append(self.weights, self.edge).astype(np.float64).tobytes()

    def load_weights(self, blob: bytes) -> None:
        w = np.frombuffer(blob, dtype=np.float64)
        if w.size != N_FEATURES + 2:
            raise ValueError(
                f"weights are {w.size} long, expected {N_FEATURES + 2}")
        self.weights = w[:-1].copy()
        self.edge = float(w[-1])
