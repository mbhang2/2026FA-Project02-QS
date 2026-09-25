"""Auction Market Theory features with dynamic bracket and time exit.

Modifies the best prior (#0) by:
- Increasing decision interval to 3 minutes and horizon to 4 decisions (12 min)
  to reduce noise and commission.
- Using a dynamic bracket based on recent 1-second volatility, with caps.
- Adding a 30-minute time exit to avoid holding stale positions.
- Trade fraction increased to 0.15 for more exposure while keeping the model
  from over-trading.

The core remains ridge regression on the five AMT features, with a reservoir
for threshold setting. Feature standardization is not used because #3 showed
it did not help and added complexity. The dynamic bracket uses average absolute
1-second price change over the last 300 bars, scaled and capped.
"""

from __future__ import annotations

from collections import deque

import numpy as np

# -- registered feature set -------------------------------------------------- #
FEATURES = ("dist_vpoc", "va_pos", "at_lvn", "cvd_div", "tape_velocity")
N_FEATURES = 5

# -- hyperparameters ---------------------------------------------------------- #
INTERVAL = "1s"          # 1-second bars for profile construction
DECIDE_EVERY = 180       # decision every 3 minutes
HORIZON_STEPS = 4        # label lookahead in decisions -> 12 minutes
WARMUP_BARS = 600        # per session, before profile is meaningful
RIDGE_LAMBDA = 0.5
TRADE_FRACTION = 0.15    # act on top 15% of predictions by |prediction|
RESERVOIR = 8_000
SIZE = 1
TICK_SIZE = 0.25
MAX_HOLD_NS = 30 * 60 * 1_000_000_000  # 30 minutes in nanoseconds
VOL_LOOKBACK = 300       # bars for volatility estimate
VOL_STOP_MULT = 2.0      # stop = mult * avg_abs_change
VOL_TARGET_MULT = 1.5    # target = target_mult * stop
STOP_MIN = 4.0
STOP_MAX = 20.0


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

        # price history for volatility
        self.price_deque: deque = deque(maxlen=VOL_LOOKBACK)

        # label buffer: one entry per decision
        self.pending: deque = deque(maxlen=HORIZON_STEPS + 1)

        # normal equations
        self.XtX = np.zeros((N_FEATURES + 1, N_FEATURES + 1))
        self.Xty = np.zeros(N_FEATURES + 1)
        self.rows_used = 0
        self.rows_dropped = 0

        # reservoir for threshold
        self.reservoir: list = []
        self.rows_seen = 0
        self._rng = np.random.default_rng(64_001)

        self.weights: np.ndarray | None = None
        self.edge: float | None = None

        # time exit state
        self.entry_ts: int | None = None

    # -- walk ---------------------------------------------------------------- #
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
        self.price_deque.append(bar.close)

        # decision only at cadence
        if self.bars_in_session % DECIDE_EVERY:
            return

        x = self._features(bar)
        self._retire_labels(bar)
        if x is not None:
            self.pending.append((x, bar.close, bar.session))

        # time exit check (only when fitted and actionable)
        if (self.weights is not None and self.edge is not None
                and ctx.actionable and ctx.position and self.entry_ts is not None):
            if ctx.ts - self.entry_ts > MAX_HOLD_NS:
                ctx.close()
                return

        if self.weights is None or self.edge is None:
            return                            # fit window
        if not ctx.actionable or x is None:
            return
        if ctx.position or ctx.working_order_ids:
            return

        # dynamic bracket from recent volatility
        stop = self._dynamic_stop()
        target = stop * VOL_TARGET_MULT

        predicted = float(np.dot(self.weights, np.append(x, 1.0)))
        edge = self.edge
        if predicted > edge:
            ctx.buy(size=SIZE, stop_offset=stop,
                    target_offset=target, tag="amt-long")
        elif predicted < -edge:
            ctx.sell(size=SIZE, stop_offset=stop,
                     target_offset=target, tag="amt-short")

    def on_fill(self, ctx) -> None:
        # record entry time when position becomes non-zero
        if ctx.position != 0:
            self.entry_ts = ctx.ts
        else:
            self.entry_ts = None

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
        self.price_deque.clear()

    def _features(self, bar):
        """The five AMT primitives, or None if not computable."""
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

        return np.array([dist / 100.0, float(va_pos), at_lvn, cvd_div, velocity])

    def _dynamic_stop(self) -> float:
        """Average absolute 1-second price change over recent bars, scaled."""
        if len(self.price_deque) < 2:
            return STOP_MIN
        diffs = np.abs(np.diff(np.asarray(self.price_deque)))
        avg_abs = float(np.mean(diffs))
        stop = avg_abs * VOL_STOP_MULT
        return min(max(stop, STOP_MIN), STOP_MAX)

    # -- training ------------------------------------------------------------ #
    def _retire_labels(self, bar) -> None:
        """Turn oldest pending row into a label when its future has arrived."""
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
        """Ridge regression solved from normal equations; set threshold."""
        if self.rows_used < 1000:
            raise RuntimeError(
                f"fit() reached with {self.rows_used} usable rows, far below "
                f"expectation. {self.rows_dropped} dropped at boundaries. "
                "Check features and horizon.")
        ridge = RIDGE_LAMBDA * np.eye(N_FEATURES + 1)
        ridge[-1, -1] = 0.0
        self.weights = np.linalg.solve(self.XtX + ridge, self.Xty)

        preds = np.abs(np.asarray(self.reservoir) @ self.weights)
        self.edge = float(np.quantile(preds, 1.0 - TRADE_FRACTION))

    # -- serialization ------------------------------------------------------- #
    def save_weights(self) -> bytes:
        if self.weights is None or self.edge is None:
            raise RuntimeError("save_weights() before fit()")
        return np.append(self.weights, self.edge).astype(np.float64).tobytes()

    def load_weights(self, blob: bytes) -> None:
        w = np.frombuffer(blob, dtype=np.float64)
        if w.size != N_FEATURES + 2:
            raise ValueError(f"expected {N_FEATURES + 2} weights, got {w.size}")
        self.weights = w[:-1].copy()
        self.edge = float(w[-1])
