"""Auction Market Theory features, supervised, with cost-aware trading.

Improves on trial #0 by:
- Restricting training data to the RTH trading window, so the model learns
  relationships that hold during the hours it actually trades, rather than
  including the thin overnight session.
- Extending the prediction horizon from 1 decision (5 minutes) to 2 decisions
  (10 minutes), which reduces noise in the label and may capture more
  meaningful moves while still fitting within the session.
- Keeping the asymmetric bracket (stop 6, target 12) and the minimum
  prediction threshold of 1.0 points to ensure trades clear costs.

All other logic (feature computation, ridge regression, reservoir threshold,
RTH trading window, force-close before session end) is unchanged.
"""

from __future__ import annotations

from collections import deque

import numpy as np

# -- hyperparameters --------------------------------------------------------- #
INTERVAL = "1s"
DECIDE_EVERY = 300       # one decision every 5 minutes
HORIZON_STEPS = 2        # label lookahead = 2 decisions (10 minutes)
WARMUP_BARS = 600        # per session, before profile is meaningful
RIDGE_LAMBDA = 1.0
TRADE_FRACTION = 0.02    # act on top 2% of decisions by |prediction|
MIN_PREDICTION = 1.0     # absolute minimum predicted return in points
RESERVOIR = 4_000
STOP_POINTS = 6.0
TARGET_POINTS = 12.0
SIZE = 1
TICK_SIZE = 0.25

# RTH window in seconds after session start (17:00 CT previous day)
TRADE_START_ELAPSED = 15.5 * 3600   # 8:30 CT
TRADE_END_ELAPSED = 22.25 * 3600    # 15:15 CT
CLOSE_ELAPSED = 22.5 * 3600         # 15:30 CT, 30 min before close


class Strategy:
    def __init__(self) -> None:
        from src.core.features.volume_profile import SessionProfile

        self.profile = SessionProfile(TICK_SIZE)
        self.session: int | None = None
        self.bars_in_session = 0

        # session state for features
        self.cum_delta = 0
        self.session_open: float | None = None
        self.tick_count_sum = 0
        self.volume_sum = 0

        # label buffer: one entry per decision
        self.pending: deque = deque(maxlen=HORIZON_STEPS + 1)

        # normal equations for ridge regression
        self.XtX = np.zeros((5 + 1, 5 + 1))
        self.Xty = np.zeros(5 + 1)
        self.rows_used = 0
        self.rows_dropped = 0

        # reservoir sample for threshold calibration
        self.reservoir: list = []
        self.rows_seen = 0
        self._rng = np.random.default_rng(64_001)

        self.weights: np.ndarray | None = None
        self.edge: float | None = None

    # -- walk ----------------------------------------------------------------- #
    def setup(self, ctx) -> None:
        ctx.subscribe(INTERVAL)

    def on_bars(self, ctx) -> None:
        history = ctx.history(INTERVAL)
        if not history:
            return
        bar = history[-1]

        # session change: reset per-session state
        if bar.session != self.session:
            self._new_session(bar)

        # update session aggregates
        self.profile.add_bar(bar.low, bar.high, bar.volume)
        self.cum_delta += bar.delta
        self.tick_count_sum += bar.tick_count
        self.volume_sum += bar.volume
        self.bars_in_session += 1

        # force-close if we are holding and near session end
        elapsed = ctx.ts - ctx.session_start
        if ctx.position and elapsed > CLOSE_ELAPSED * 1_000_000_000:
            if ctx.working_order_ids:
                ctx.cancel_all()
            ctx.close()
            return

        # only make decisions at the cadence
        if self.bars_in_session % DECIDE_EVERY != 0:
            return

        # compute features at decision time
        x = self._features(bar)

        # determine if we are inside the RTH window (for training and trading)
        in_rth = (elapsed >= TRADE_START_ELAPSED * 1_000_000_000 and
                  elapsed <= TRADE_END_ELAPSED * 1_000_000_000)

        # retire labels and accumulate training rows only for RTH decisions
        self._retire_labels(bar, accumulate=in_rth)

        if x is not None:
            self.pending.append((x, bar.close, bar.session))

        # trading logic only after fit and during actionable, inside RTH window
        if self.weights is None or self.edge is None:
            return
        if not ctx.actionable or x is None:
            return
        if ctx.position or ctx.working_order_ids:
            return
        if not in_rth:
            return

        predicted = float(np.dot(self.weights, np.append(x, 1.0)))
        threshold = max(self.edge, MIN_PREDICTION)
        if predicted > threshold:
            ctx.buy(size=SIZE, stop_offset=STOP_POINTS,
                    target_offset=TARGET_POINTS, tag="amt-long")
        elif predicted < -threshold:
            ctx.sell(size=SIZE, stop_offset=STOP_POINTS,
                     target_offset=TARGET_POINTS, tag="amt-short")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    # -- features ------------------------------------------------------------- #
    def _new_session(self, bar) -> None:
        self.profile.reset()
        self.session = bar.session
        self.bars_in_session = 0
        self.cum_delta = 0
        self.tick_count_sum = 0
        self.volume_sum = 0
        self.session_open = bar.open

    def _features(self, bar):
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

        bars = max(self.bars_in_session, 1)
        mean_ticks = self.tick_count_sum / bars
        mean_volume = self.volume_sum / bars
        v_trades = (bar.tick_count / mean_ticks - 1.0) if mean_ticks > 0 else 0.0
        v_volume = (bar.volume / mean_volume - 1.0) if mean_volume > 0 else 0.0
        velocity = 0.5 * (v_trades + v_volume)

        return np.array([dist / 100.0, float(va_pos), at_lvn, cvd_div, velocity])

    # -- training ------------------------------------------------------------- #
    def _retire_labels(self, bar, accumulate: bool) -> None:
        if self.weights is not None or len(self.pending) < HORIZON_STEPS:
            return
        x, price, session = self.pending[0]
        self.pending.popleft()
        if session != bar.session:
            self.rows_dropped += 1
            return
        y = bar.close - price
        if accumulate:
            row = np.append(x, 1.0)
            self.XtX += np.outer(row, row)
            self.Xty += row * y
            self.rows_used += 1
            self.rows_seen += 1
            if len(self.reservoir) < RESERVOIR:
                self.reservoir.append(row)
            else:
                j = int(self._rng.integers(0, self.rows_seen))
                if j < RESERVOIR:
                    self.reservoir[j] = row

    def fit(self) -> None:
        if self.rows_used < 1000:
            raise RuntimeError(
                f"fit() reached with {self.rows_used} usable rows, which is far "
                f"below what a {HORIZON_STEPS}-step horizon over the fit window "
                f"implies. {self.rows_dropped} were dropped at session boundaries. "
                "Do not fit a model to this.")
        ridge = RIDGE_LAMBDA * np.eye(6)
        ridge[-1, -1] = 0.0
        self.weights = np.linalg.solve(self.XtX + ridge, self.Xty)

        preds = np.abs(np.asarray(self.reservoir) @ self.weights)
        self.edge = float(np.quantile(preds, 1.0 - TRADE_FRACTION))

    # -- persistence ---------------------------------------------------------- #
    def save_weights(self) -> bytes:
        if self.weights is None or self.edge is None:
            raise RuntimeError("save_weights() before fit()")
        return np.append(self.weights, self.edge).astype(np.float64).tobytes()

    def load_weights(self, blob: bytes) -> None:
        w = np.frombuffer(blob, dtype=np.float64)
        if w.size != 7:
            raise ValueError(f"weights are {w.size} long, expected 7")
        self.weights = w[:-1].copy()
        self.edge = float(w[-1])
