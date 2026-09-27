"""Auction Market Theory features, supervised.

Enhancements over the champion:
1. Added short-term CVD divergence (1-minute flow vs 1-minute move) to capture immediate order flow imbalance alongside session-long context.
2. Stabilized tape velocity by comparing the 1-minute average to the session average, instead of the noisy 1-second bar to the session average.
3. Increased TRADE_FRACTION to 7.5% to ensure sufficient session exposure (clearing the 70% threshold).
4. Increased bracket size to 10 points to allow more room for the 5-minute prediction horizon to play out.
"""

from __future__ import annotations

from collections import deque

import numpy as np

FEATURES = ("dist_vpoc", "va_pos", "at_lvn", "cvd_div", "tape_velocity", "st_cvd_div")
N_FEATURES = 6

INTERVAL = "1s"
DECIDE_EVERY = 60
HORIZON_STEPS = 5
WARMUP_BARS = 600
RIDGE_LAMBDA = 1.0
TRADE_FRACTION = 0.075
RESERVOIR = 4_000
STOP_POINTS = 10.0
TARGET_POINTS = 10.0
SIZE = 1
TICK_SIZE = 0.25


class Strategy:
    def __init__(self) -> None:
        from src.core.features.volume_profile import SessionProfile

        self.profile = SessionProfile(TICK_SIZE)
        self.session: int | None = None
        self.bars_in_session = 0

        self.cum_delta = 0
        self.session_open: float | None = None
        self.tick_count_sum = 0
        self.volume_sum = 0

        self.st_delta = 0
        self.st_volume = 0
        self.st_tick_count = 0
        self.last_decision_price: float | None = None

        self.pending: deque = deque(maxlen=HORIZON_STEPS + 1)

        self.XtX = np.zeros((N_FEATURES + 1, N_FEATURES + 1))
        self.Xty = np.zeros(N_FEATURES + 1)
        self.rows_used = 0
        self.rows_dropped = 0

        self.reservoir: list = []
        self.rows_seen = 0
        self._rng = np.random.default_rng(64_001)

        self.weights: np.ndarray | None = None
        self.edge: float | None = None

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
        self.volume_sum += bar.volume
        self.bars_in_session += 1

        self.st_delta += bar.delta
        self.st_volume += bar.volume
        self.st_tick_count += bar.tick_count

        if self.bars_in_session % DECIDE_EVERY:
            return

        x = self._features(bar)
        
        self.st_delta = 0
        self.st_volume = 0
        self.st_tick_count = 0
        self.last_decision_price = bar.close

        self._retire_labels(bar)
        if x is not None:
            self.pending.append((x, bar.close, bar.session))

        if self.weights is None or self.edge is None:
            return
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

    def _new_session(self, bar) -> None:
        self.profile.reset()
        self.session = bar.session
        self.bars_in_session = 0
        self.cum_delta = 0
        self.tick_count_sum = 0
        self.volume_sum = 0
        self.session_open = bar.open
        self.st_delta = 0
        self.st_volume = 0
        self.st_tick_count = 0
        self.last_decision_price = None

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

        st_mean_ticks = self.st_tick_count / DECIDE_EVERY
        st_mean_volume = self.st_volume / DECIDE_EVERY

        v_trades = (st_mean_ticks / mean_ticks - 1.0) if mean_ticks > 0 else 0.0
        v_volume = (st_mean_volume / mean_volume - 1.0) if mean_volume > 0 else 0.0
        velocity = 0.5 * (v_trades + v_volume)

        st_span = max(self.st_volume, 1)
        st_flow = self.st_delta / st_span
        st_move = 0.0
        if self.last_decision_price is not None:
            st_move = (bar.close - self.last_decision_price) / max(abs(self.last_decision_price), 1.0)
        st_cvd_div = st_flow - st_move * 100.0

        return np.array([dist / 100.0, float(va_pos), at_lvn, cvd_div, velocity, st_cvd_div])

    def _retire_labels(self, bar) -> None:
        if self.weights is not None or len(self.pending) < HORIZON_STEPS:
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
        if self.rows_used < 1000:
            raise RuntimeError(
                f"fit() reached with {self.rows_used} usable rows, which is far "
                f"below what a {HORIZON_STEPS}-step horizon over the fit window "
                f"implies. "
                f"{self.rows_dropped} were dropped at session boundaries. Either "
                "the window is too short or the features are returning None "
                "everywhere -- do not fit a model to this.")
        ridge = RIDGE_LAMBDA * np.eye(N_FEATURES + 1)
        ridge[-1, -1] = 0.0
        self.weights = np.linalg.solve(self.XtX + ridge, self.Xty)

        preds = np.abs(np.asarray(self.reservoir) @ self.weights)
        self.edge = float(np.quantile(preds, 1.0 - TRADE_FRACTION))

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
