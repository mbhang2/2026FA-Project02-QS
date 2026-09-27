"""Auction Market Theory features, supervised.

Enhancements over trial #5 (best performing trial):
1. Maintained the successful core configuration from #5: DECIDE_EVERY=300 (5m), HORIZON_STEPS=6 (30m), target clipping to [-100, 100], and unclipped features.
2. Added `va_flow_agree` and `va_st_flow_agree` (interaction between Value Area position and order flow). This captures a core AMT concept: order flow pushing away from the Value Area indicates a breakout/trend, while flow pushing towards it indicates mean-reversion.
3. Added `vwap_cross_bp` to provide short-term vs session trend context.
4. Set TRADE_FRACTION to 0.11 and increased RIDGE_LAMBDA to 1500.0 to balance the added features while safely ensuring >70% session exposure.
5. Avoided direct volatility features (like session range) which caused previous trials to cluster trades in a small number of volatile sessions and fail the exposure floor.
"""

from __future__ import annotations

from collections import deque

import numpy as np

FEATURES = (
    "va_pos", "dist_va_high", "dist_va_low", "dist_vpoc", "at_lvn",
    "flow", "move", "cvd_div", "st_flow", "st_move", "st_cvd_div",
    "tape_velocity_trades", "tape_velocity_volume", "dist_vwap", "dist_st_vwap",
    "va_flow_agree", "va_st_flow_agree", "vwap_cross"
)
N_FEATURES = 18

INTERVAL = "1s"
DECIDE_EVERY = 300
HORIZON_STEPS = 6
WARMUP_BARS = 1800
RIDGE_LAMBDA = 1500.0
TRADE_FRACTION = 0.11
STOP_POINTS = 25.0
TARGET_POINTS = 40.0
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
        self.cum_pv = 0.0

        self.st_delta = 0
        self.st_volume = 0
        self.st_tick_count = 0
        self.st_cum_pv = 0.0
        self.last_decision_price: float | None = None

        self.pending: deque = deque(maxlen=HORIZON_STEPS + 1)

        self.X_train: list = []
        self.y_train: list = []
        self.rows_used = 0
        self.rows_dropped = 0

        self.X_mean: np.ndarray | None = None
        self.X_std: np.ndarray | None = None
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
        self.cum_pv += bar.close * bar.volume
        self.bars_in_session += 1

        self.st_delta += bar.delta
        self.st_volume += bar.volume
        self.st_tick_count += bar.tick_count
        self.st_cum_pv += bar.close * bar.volume

        if self.bars_in_session % DECIDE_EVERY:
            return

        x = self._features(bar)
        
        self.st_delta = 0
        self.st_volume = 0
        self.st_tick_count = 0
        self.st_cum_pv = 0.0
        self.last_decision_price = bar.close

        if self.weights is None:
            self._retire_labels(bar)
            time_left = ctx.session_end - ctx.ts
            if x is not None and time_left >= 35 * 60 * 1_000_000_000:
                self.pending.append((x, bar.close, bar.session))

        if self.weights is None or self.edge is None:
            return
        if not ctx.actionable or x is None:
            return

        # End of session flattening
        time_left = ctx.session_end - ctx.ts
        if time_left < 5 * 60 * 1_000_000_000:
            if ctx.position or ctx.working_order_ids:
                if not ctx.find_orders(tag="eos-close"):
                    ctx.cancel_all()
                    if ctx.position > 0:
                        ctx.sell(size=ctx.position, type="market", reduce_only=True, tag="eos-close")
                    elif ctx.position < 0:
                        ctx.buy(size=abs(ctx.position), type="market", reduce_only=True, tag="eos-close")
            return

        if ctx.position or ctx.working_order_ids:
            return

        # Do not enter new trades if there isn't enough time for the horizon to play out
        if time_left < 35 * 60 * 1_000_000_000:
            return

        x_scaled = (x - self.X_mean) / self.X_std
        x_scaled = np.append(x_scaled, 1.0)
        predicted = float(np.dot(self.weights, x_scaled))

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
        self.cum_pv = 0.0
        self.session_open = bar.open
        self.st_delta = 0
        self.st_volume = 0
        self.st_tick_count = 0
        self.st_cum_pv = 0.0
        self.last_decision_price = None
        self.pending.clear()

    def _features(self, bar):
        if self.bars_in_session < WARMUP_BARS or self.profile.is_empty:
            return None
        
        band = self.profile.value_area()
        if band is None:
            return None
        va_low, va_high = band

        va_pos = self.profile.value_area_position(bar.close)
        if va_pos is None:
            return None

        dist_va_high_bp = (bar.close - va_high) / max(va_high, 1.0) * 10000.0
        dist_va_low_bp = (bar.close - va_low) / max(va_low, 1.0) * 10000.0
        
        vpoc = self.profile.vpoc()
        dist_vpoc_bp = 0.0
        if vpoc is not None:
            dist_vpoc_bp = (bar.close - vpoc) / max(vpoc, 1.0) * 10000.0

        nodes = self.profile.low_volume_nodes()
        at_lvn = 1.0 if any(abs(bar.close - n) <= 2 * TICK_SIZE for n in nodes) else 0.0

        span = max(self.profile.total_volume, 1)
        flow = self.cum_delta / span
        move = (bar.close - self.session_open) / max(abs(self.session_open), 1.0) * 10000.0
        cvd_div = flow - move / 100.0

        bars = max(self.bars_in_session, 1)
        mean_ticks = self.tick_count_sum / bars
        mean_volume = self.volume_sum / bars
        
        st_mean_ticks = self.st_tick_count / DECIDE_EVERY
        st_mean_volume = self.st_volume / DECIDE_EVERY
        
        v_trades = (st_mean_ticks / mean_ticks - 1.0) if mean_ticks > 0 else 0.0
        v_volume = (st_mean_volume / mean_volume - 1.0) if mean_volume > 0 else 0.0
        
        st_span = max(self.st_volume, 1)
        st_flow = self.st_delta / st_span
        st_move = 0.0
        if self.last_decision_price is not None:
            st_move = (bar.close - self.last_decision_price) / max(abs(self.last_decision_price), 1.0) * 10000.0
        
        st_cvd_div = st_flow - st_move / 100.0

        if self.volume_sum > 0:
            vwap = self.cum_pv / self.volume_sum
        else:
            vwap = bar.close
        dist_vwap_bp = (bar.close - vwap) / max(vwap, 1.0) * 10000.0

        if self.st_volume > 0:
            st_vwap = self.st_cum_pv / self.st_volume
        else:
            st_vwap = bar.close
        dist_st_vwap_bp = (bar.close - st_vwap) / max(st_vwap, 1.0) * 10000.0

        vwap_cross_bp = (st_vwap - vwap) / max(vwap, 1.0) * 10000.0

        return np.array([
            float(va_pos),
            dist_va_high_bp,
            dist_va_low_bp,
            dist_vpoc_bp,
            at_lvn,
            flow,
            move,
            cvd_div,
            st_flow,
            st_move,
            st_cvd_div,
            v_trades,
            v_volume,
            dist_vwap_bp,
            dist_st_vwap_bp,
            float(va_pos) * flow,
            float(va_pos) * st_flow,
            vwap_cross_bp
        ], dtype=np.float64)

    def _retire_labels(self, bar) -> None:
        if len(self.pending) < HORIZON_STEPS:
            return
        x, price, session = self.pending[0]
        if session != bar.session:
            self.pending.popleft()
            self.rows_dropped += 1
            return
        y = bar.close - price
        self.X_train.append(x)
        self.y_train.append(y)
        self.rows_used += 1
        self.pending.popleft()

    def fit(self) -> None:
        if self.rows_used < 1000:
            raise RuntimeError(
                f"fit() reached with {self.rows_used} usable rows, which is far "
                f"below what a {HORIZON_STEPS}-step horizon over the fit window "
                f"implies. "
                f"{self.rows_dropped} were dropped at session boundaries."
            )
        
        X = np.array(self.X_train, dtype=np.float64)
        y = np.array(self.y_train, dtype=np.float64)
        
        # Clip targets to reduce outlier influence on Ridge
        y = np.clip(y, -100.0, 100.0)
        
        self.X_mean = np.mean(X, axis=0)
        self.X_std = np.std(X, axis=0) + 1e-8
        
        X_scaled = (X - self.X_mean) / self.X_std
        X_scaled = np.column_stack([X_scaled, np.ones(len(X_scaled))])
        
        XtX = X_scaled.T @ X_scaled
        Xty = X_scaled.T @ y
        
        ridge = RIDGE_LAMBDA * np.eye(N_FEATURES + 1)
        ridge[-1, -1] = 0.0
        
        self.weights = np.linalg.solve(XtX + ridge, Xty)
        
        preds = X_scaled @ self.weights
        self.edge = float(np.quantile(np.abs(preds), 1.0 - TRADE_FRACTION))
        
        self.X_train.clear()
        self.y_train.clear()

    def save_weights(self) -> bytes:
        if self.weights is None or self.edge is None or self.X_mean is None or self.X_std is None:
            raise RuntimeError("save_weights() before fit()")
        blob = np.concatenate([
            self.X_mean,
            self.X_std,
            self.weights,
            [self.edge]
        ]).astype(np.float64).tobytes()
        return blob

    def load_weights(self, blob: bytes) -> None:
        w = np.frombuffer(blob, dtype=np.float64)
        expected_size = N_FEATURES + N_FEATURES + (N_FEATURES + 1) + 1
        if w.size != expected_size:
            raise ValueError(
                f"weights are {w.size} long, expected {expected_size}")
        
        idx = 0
        self.X_mean = w[idx : idx + N_FEATURES]
        idx += N_FEATURES
        self.X_std = w[idx : idx + N_FEATURES]
        idx += N_FEATURES
        self.weights = w[idx : idx + N_FEATURES + 1]
        idx += N_FEATURES + 1
        self.edge = float(w[idx])
