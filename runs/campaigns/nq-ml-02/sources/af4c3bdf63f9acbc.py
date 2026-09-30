"""Auction Market Theory features, supervised. Seventeenth variation.

Changes from the best variation (#2, score +1.7807):

- **Feature standardization** added before ridge regression. The raw features
  have different scales (e.g., distance/100 vs. velocity ratio), and ridge
  regularization penalizes coefficients uniformly. Standardizing each feature
  to zero mean and unit variance during fitting ensures the regularizer treats
  them fairly and can improve numerical conditioning and predictive
  performance.

All other hyperparameters, feature set, ridge regression, reservoir sampling,
and memory strategy are identical to #2.
"""

from __future__ import annotations

from collections import deque

import numpy as np

# -- feature set ------------------------------------------------------------- #
# Original five plus momentum.
N_FEATURES = 6

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

        # rolling window buffers
        self.delta_window: deque[float] = deque(maxlen=ROLLING_WINDOW)
        self.close_window: deque[float] = deque(maxlen=ROLLING_WINDOW)

        # label buffer
        self.pending: deque = deque(maxlen=HORIZON_STEPS + 1)

        # training rows (feature vector and label)
        self.training_rows: list = []
        self.rows_used = 0
        self.rows_dropped = 0

        # reservoir for threshold (stores raw feature vectors)
        self.reservoir: list = []
        self.rows_seen = 0
        self._rng = np.random.default_rng(64_001)

        # fitted parameters
        self.weights: np.ndarray | None = None
        self.intercept: float | None = None
        self.feature_mean: np.ndarray | None = None
        self.feature_std: np.ndarray | None = None
        self.edge: float | None = None

    # -- walk ----------------------------------------------------------------- #
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

        # update rolling windows
        self.delta_window.append(bar.delta)
        self.close_window.append(bar.close)

        if self.bars_in_session % DECIDE_EVERY:
            return

        x = self._features(bar)
        self._retire_labels(bar)
        if x is not None:
            self.pending.append((x, bar.close, bar.session))

        if self.weights is None or self.edge is None:
            return
        if not ctx.actionable or x is None:
            return
        if ctx.position or ctx.working_order_ids:
            return

        predicted = self._predict(x)
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

        return np.array([
            dist / 100.0,
            float(va_pos),
            at_lvn,
            cvd_div,
            velocity,
            momentum / 100.0,
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
        self.training_rows.append((x, y))
        self.rows_used += 1
        self.pending.popleft()

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

        X_raw = np.array([r[0] for r in self.training_rows])
        y = np.array([r[1] for r in self.training_rows])

        # Standardization parameters
        self.feature_mean = X_raw.mean(axis=0)
        self.feature_std = X_raw.std(axis=0) + 1e-8

        # Standardize features
        X = (X_raw - self.feature_mean) / self.feature_std
        # Add bias column
        X_design = np.column_stack([X, np.ones(X.shape[0])])

        # Ridge regression with no regularization on bias
        ridge = RIDGE_LAMBDA * np.eye(X_design.shape[1])
        ridge[-1, -1] = 0.0
        beta = np.linalg.solve(X_design.T @ X_design + ridge,
                               X_design.T @ y)
        self.weights = beta[:-1]
        self.intercept = beta[-1]

        # Compute edge from reservoir predictions (standardized)
        if self.reservoir:
            reservoir_raw = np.array(self.reservoir)
            reservoir_std = (reservoir_raw - self.feature_mean) / self.feature_std
            preds = reservoir_std @ self.weights + self.intercept
            abs_preds = np.abs(preds)
            quantile_edge = float(np.quantile(abs_preds, 1.0 - TRADE_FRACTION))
            self.edge = max(quantile_edge, MIN_EDGE_ABS)
        else:
            self.edge = MIN_EDGE_ABS

    def _predict(self, x_raw: np.ndarray) -> float:
        # Ensure all required attributes are not None
        assert self.weights is not None
        assert self.intercept is not None
        assert self.feature_mean is not None
        assert self.feature_std is not None
        x_std = (x_raw - self.feature_mean) / self.feature_std
        return float(np.dot(self.weights, x_std) + self.intercept)

    # -- persistence ---------------------------------------------------------- #
    def save_weights(self) -> bytes:
        if (self.weights is None or self.intercept is None or
                self.feature_mean is None or self.feature_std is None or
                self.edge is None):
            raise RuntimeError("save_weights() before fit()")
        blob = np.concatenate([
            [self.edge],
            [self.intercept],
            self.weights,
            self.feature_mean,
            self.feature_std,
        ]).astype(np.float64).tobytes()
        return blob

    def load_weights(self, blob: bytes) -> None:
        data = np.frombuffer(blob, dtype=np.float64)
        expected_len = 1 + 1 + N_FEATURES + N_FEATURES + N_FEATURES
        if data.size != expected_len:
            raise ValueError(
                f"weights are {data.size} long, expected {expected_len}")
        idx = 0
        self.edge = float(data[idx]); idx += 1
        self.intercept = float(data[idx]); idx += 1
        self.weights = data[idx:idx+N_FEATURES].copy(); idx += N_FEATURES
        self.feature_mean = data[idx:idx+N_FEATURES].copy(); idx += N_FEATURES
        self.feature_std = data[idx:idx+N_FEATURES].copy()
