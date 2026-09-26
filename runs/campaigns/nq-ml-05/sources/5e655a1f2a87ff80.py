"""Auction Market Theory features, supervised.

Implements `config/hypotheses/auction-market-features.md`: ViperQ's feature set
with a supervised forward-return target.

Improvements over prior trials:
1. Horizon adjusted to 10 minutes (HORIZON_STEPS=20 with 30s decisions).
2. Added VWAP distance to features (N_FEATURES=13).
3. Increased Ridge lambda to 10000.0 for stronger regularization against 1s noise.
4. Bracket adjusted to 15.0 stop / 30.0 target for 1:2 R:R.
5. TRADE_FRACTION set to 0.015 with a hard minimum edge of 0.5 bps to ensure 
   we only take trades that can cover friction.
"""

from __future__ import annotations

from collections import deque

import numpy as np


FEATURES = (
    "dist_vpoc_bps", "va_pos", "at_lvn", "flow", "flow_1m", "flow_5m",
    "move_bps", "ret_1m", "ret_5m", "velocity", "inter_session", "inter_5m",
    "dist_vwap_bps"
)
N_FEATURES = 13

INTERVAL = "1s"
DECIDE_EVERY = 30
HORIZON_STEPS = 20       # 20 * 30s = 10 minutes
WARMUP_BARS = 360        # 6 minutes
RIDGE_LAMBDA = 10000.0
TRADE_FRACTION = 0.015
RESERVOIR = 20_000
STOP_POINTS = 15.0
TARGET_POINTS = 30.0
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
        self.cum_price_vol = 0.0

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
        self.cum_price_vol += bar.close * bar.volume
        self.bars_in_session += 1

        if self.bars_in_session % DECIDE_EVERY:
            return

        x = self._features(bar, history)
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
        edge = max(self.edge, 0.5)
        
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
        self.cum_price_vol = 0.0
        self.session_open = bar.open

    def _features(self, bar, history):
        if self.bars_in_session < WARMUP_BARS or self.profile.is_empty:
            return None
        band = self.profile.value_area()
        if band is None:
            return None

        dist = self.profile.distance_from_vpoc(bar.close)
        if dist is None:
            return None
        dist_vpoc_bps = dist / bar.close * 10000.0

        va_pos = self.profile.value_area_position(bar.close)

        nodes = self.profile.low_volume_nodes()
        at_lvn = 1.0 if any(abs(bar.close - n) <= TICK_SIZE for n in nodes) else 0.0

        span = max(self.profile.total_volume, 1)
        flow = self.cum_delta / span
        move_bps = (bar.close - self.session_open) / max(abs(self.session_open), 1.0) * 10000.0

        if len(history) >= 60:
            recent_60 = history.last(60)
            delta_1m = sum(b.delta for b in recent_60)
            vol_1m = sum(b.volume for b in recent_60)
            flow_1m = delta_1m / max(vol_1m, 1)
            past_bar_1m = recent_60[0]
            ret_1m = (bar.close - past_bar_1m.close) / past_bar_1m.close * 10000.0
        else:
            flow_1m = 0.0
            ret_1m = 0.0

        if len(history) >= 300:
            recent_300 = history.last(300)
            delta_5m = sum(b.delta for b in recent_300)
            vol_5m = sum(b.volume for b in recent_300)
            flow_5m = delta_5m / max(vol_5m, 1)
            past_bar_5m = recent_300[0]
            ret_5m = (bar.close - past_bar_5m.close) / past_bar_5m.close * 10000.0
        else:
            flow_5m = 0.0
            ret_5m = 0.0

        if len(history) >= 60:
            recent_60 = history.last(60)
            recent_ticks = sum(b.tick_count for b in recent_60) / 60.0
            recent_vol = sum(b.volume for b in recent_60) / 60.0
        else:
            recent_ticks = bar.tick_count
            recent_vol = bar.volume

        bars = max(self.bars_in_session, 1)
        mean_ticks = self.tick_count_sum / bars
        mean_volume = self.volume_sum / bars

        v_trades = (recent_ticks / mean_ticks - 1.0) if mean_ticks > 0 else 0.0
        v_volume = (recent_vol / mean_volume - 1.0) if mean_volume > 0 else 0.0
        velocity = float(np.clip(0.5 * (v_trades + v_volume), -1.0, 10.0))

        inter_session = flow * (move_bps / 100.0)
        inter_5m = flow_5m * (ret_5m / 20.0)
        
        vwap = self.cum_price_vol / max(self.volume_sum, 1)
        dist_vwap_bps = (bar.close - vwap) / vwap * 10000.0

        return np.array([
            dist_vpoc_bps / 50.0,
            float(va_pos),
            at_lvn,
            flow,
            flow_1m,
            flow_5m,
            move_bps / 100.0,
            ret_1m / 10.0,
            ret_5m / 20.0,
            velocity / 5.0,
            inter_session,
            inter_5m,
            dist_vwap_bps / 50.0
        ])

    def _retire_labels(self, bar) -> None:
        if self.weights is not None or len(self.pending) < HORIZON_STEPS:
            return
        x, price, session = self.pending[0]
        if session != bar.session:
            self.pending.popleft()
            self.rows_dropped += 1
            return
        y = (bar.close - price) / price * 10000.0
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
