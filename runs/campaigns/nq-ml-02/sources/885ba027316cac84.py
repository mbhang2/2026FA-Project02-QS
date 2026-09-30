"""Auction Market Theory features, supervised. Eleventh variation.

Changes from the best variation (#2, score +1.7807):

- **Feature standardization**: All features are standardized to zero mean and
  unit variance using statistics computed on the fit window. This ensures the
  ridge penalty is applied uniformly across features with different natural
  scales, which should improve coefficient estimation. The scalers are persisted
  alongside the model weights.
- **New feature: session time fraction** — the fraction of the session elapsed
  at the current bar. Auction rotations have a time-of-day structure, and this
  feature lets the model learn it. It is the 7th feature.
- **Hyperparameters unchanged** from #2: decision cadence 180s, horizon 20
  decisions, ridge lambda 20.0, trade threshold 1%, rolling window 900 bars,
  fixed bracket 5/15 points.

Everything else (feature set except the added time fraction, ridge regression,
reservoir sampling, memory strategy) is identical to #2.
"""

from __future__ import annotations

from collections import deque

import numpy as np

# -- feature set ------------------------------------------------------------- #
# Original five plus momentum plus session time fraction.
N_FEATURES = 7

# -- hyperparameters --------------------------------------------------------- #
INTERVAL = "1s"
DECIDE_EVERY = 180
HORIZON_STEPS = 20
WARMUP_BARS = 600
RIDGE_LAMBDA = 20.0
TRADE_FRACTION = 0.01
RESERVOIR = 4_000
STOP_POINTS = 5.0
TARGET_POINTS = 15.0
SIZE = 1
TICK_SIZE = 0.25
ROLLING_WINDOW = 900
MIN_EDGE_ABS = 0.3


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
        self.session_start: int | None = None
        self.session_end: int | None = None

        # rolling window buffers
        self.delta_window: deque[float] = deque(maxlen=ROLLING_WINDOW)
        self.close_window: deque[float] = deque(maxlen=ROLLING_WINDOW)

        # label buffer
        self.pending: deque = deque(maxlen=HORIZON_STEPS + 1)

        # training accumulation (raw features and labels)
        self.train_rows: list = []
        self.train_labels: list = []
        self.rows_used = 0
        self.rows_dropped = 0

        # reservoir for threshold (raw features)
        self.reservoir: list = []
        self.rows_seen = 0
        self._rng = np.random.default_rng(64_001)

        # model and scalers
        self.weights: np.ndarray | None = None
        self.edge: float | None = None
        self.mean: np.ndarray | None = None
        self.std: np.ndarray | None = None

    # -- walk ----------------------------------------------------------------- #
    def setup(self, ctx) -> None:
        ctx.subscribe(INTERVAL)

    def on_bars(self, ctx) -> None:
        history = ctx.history(INTERVAL)
        if not history:
            return
        bar = history[-1]

        if bar.session != self.session:
            self.session_start = ctx.session_start
            self.session_end = ctx.session_end
            self._new_session(bar)

        self.profile.add_bar(bar.low, bar.high, bar.volume)
        self.cum_delta += bar.delta
        self.tick_count_sum += bar.tick_count
        self.bars_in_session += 1

        # update rolling windows
        self.delta_window.append(bar.delta)
        self.close_window.append(bar.close)

        if self.bars_in_session % DECIDE_EVERY:
            return

        x = self._features(bar)
        # During training, retire labels and accumulate rows.
        if self.weights is None:
            self._retire_labels(bar)
            if x is not None:
                self.pending.append((x, bar.close, bar.session))
        else:
            # After fit, no need to accumulate pending.
            pass

        if self.weights is None or self.edge is None:
            return
        if not ctx.actionable or x is None:
            return
        if ctx.position or ctx.working_order_ids:
            return

        # Standardize features for prediction.
        x_std = (x - self.mean) / self.std
        row = np.append(x_std, 1.0)
        predicted = float(np.dot(self.weights, row))
        if predicted > self.edge:
            ctx.buy(size=SIZE, stop_offset=STOP_POINTS,
                    target_offset=TARGET_POINTS, tag="amt-long")
        elif predicted < -self.edge:
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
        self.session_open = bar.open
        self.delta_window.clear()
        self.close_window.clear()

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

        # rolling CVD divergence: flow over last ROLLING_WINDOW bars vs price move
        if len(self.delta_window) < ROLLING_WINDOW:
            return None
        roll_delta = sum(self.delta_window)
        roll_volume = sum(abs(d) for d in self.delta_window)
        if roll_volume == 0:
            roll_flow = 0.0
        else:
            roll_flow = roll_delta / roll_volume

        # price change over the same window
        if len(self.close_window) < ROLLING_WINDOW:
            return None
        price_change = bar.close - self.close_window[0]
        if abs(self.close_window[0]) < 1e-9:
            price_move = 0.0
        else:
            price_move = price_change / abs(self.close_window[0]) * 100.0

        cvd_div = roll_flow - price_move

        # tape velocity
        mean_ticks = self.tick_count_sum / max(self.bars_in_session, 1)
        velocity = (bar.tick_count / mean_ticks - 1.0) if mean_ticks > 0 else 0.0

        # momentum: return over the rolling window (in points)
        if len(self.close_window) < ROLLING_WINDOW:
            return None
        momentum = bar.close - self.close_window[0]

        # session time fraction
        if self.session_start is not None and self.session_end is not None:
            total = self.session_end - self.session_start
            if total > 0:
                time_frac = (bar.close_ts - self.session_start) / total
                time_frac = min(max(time_frac, 0.0), 1.0)
            else:
                time_frac = 0.0
        else:
            time_frac = 0.0

        return np.array([
            dist / 100.0,
            float(va_pos),
            at_lvn,
            cvd_div,
            velocity,
            momentum / 100.0,
            time_frac,
        ])

    # -- training ------------------------------------------------------------- #
    def _retire_labels(self, bar) -> None:
        if self.weights is not None or len(self.pending) <= HORIZON_STEPS:
            return
        x, price, session = self.pending[0]
        if session != bar.session:
            self.pending.popleft()
            self.rows_dropped += 1
            return
        y = bar.close - price
        # Store raw features and label.
        self.train_rows.append(x)
        self.train_labels.append(y)
        self.rows_used += 1
        self.pending.popleft()

        # Reservoir sampling of raw features for threshold calibration.
        self.rows_seen += 1
        if len(self.reservoir) < RESERVOIR:
            self.reservoir.append(x)
        else:
            j = int(self._rng.integers(0, self.rows_seen))
            if j < RESERVOIR:
                self.reservoir[j] = x

    def fit(self) -> None:
        if self.rows_used < 1000:
            raise RuntimeError(
                f"fit() reached with {self.rows_used} usable rows, which is far "
                f"below what a {HORIZON_STEPS}-step horizon over the fit window "
                f"implies. "
                f"{self.rows_dropped} were dropped at session boundaries. Either "
                "the window is too short or the features are returning None "
                "everywhere -- do not fit a model to this.")

        # Convert to arrays.
        X_raw = np.asarray(self.train_rows, dtype=np.float64)
        y = np.asarray(self.train_labels, dtype=np.float64)

        # Compute feature-wise mean and standard deviation.
        self.mean = X_raw.mean(axis=0)
        self.std = X_raw.std(axis=0)
        # Avoid division by zero for constant features.
        self.std[self.std < 1e-12] = 1.0

        # Standardize features and add bias column.
        X = (X_raw - self.mean) / self.std
        X = np.hstack([X, np.ones((X.shape[0], 1))])

        # Ridge regression.
        ridge = RIDGE_LAMBDA * np.eye(N_FEATURES + 1)
        ridge[-1, -1] = 0.0  # do not penalize bias
        self.weights = np.linalg.solve(X.T @ X + ridge, X.T @ y)

        # Calibrate trading threshold using reservoir.
        if len(self.reservoir) > 0:
            R_raw = np.asarray(self.reservoir, dtype=np.float64)
            R = (R_raw - self.mean) / self.std
            R = np.hstack([R, np.ones((R.shape[0], 1))])
            preds = np.abs(R @ self.weights)
            quantile_edge = float(np.quantile(preds, 1.0 - TRADE_FRACTION))
            self.edge = max(quantile_edge, MIN_EDGE_ABS)
        else:
            self.edge = MIN_EDGE_ABS

    # -- persistence ---------------------------------------------------------- #
    def save_weights(self) -> bytes:
        if self.weights is None or self.edge is None:
            raise RuntimeError("save_weights() before fit()")
        if self.mean is None or self.std is None:
            raise RuntimeError("save_weights() before scalers are computed")
        # Layout: weights (N_FEATURES+1), edge, mean (N_FEATURES), std (N_FEATURES)
        payload = np.concatenate([
            self.weights,
            np.array([self.edge], dtype=np.float64),
            self.mean,
            self.std,
        ])
        return payload.astype(np.float64).tobytes()

    def load_weights(self, blob: bytes) -> None:
        w = np.frombuffer(blob, dtype=np.float64)
        expected = (N_FEATURES + 1) + 1 + N_FEATURES + N_FEATURES
        if w.size != expected:
            raise ValueError(
                f"weights are {w.size} long, expected {expected}")
        idx = 0
        self.weights = w[idx:idx + N_FEATURES + 1].copy()
        idx += N_FEATURES + 1
        self.edge = float(w[idx])
        idx += 1
        self.mean = w[idx:idx + N_FEATURES].copy()
        idx += N_FEATURES
        self.std = w[idx:idx + N_FEATURES].copy()
