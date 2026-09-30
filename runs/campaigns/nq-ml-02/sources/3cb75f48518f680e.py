"""Auction Market Theory features, supervised. Standardized ridge with longer horizon and fewer trades.

Changes from the best variation (#2, score +1.7807):

- **Feature standardization**: compute mean and standard deviation of training
  features and apply to both training and prediction. Ridge regression is
  sensitive to feature scales, and the raw features have very different
  magnitudes (e.g., distance from VPOC vs momentum). Standardizing should
  improve conditioning and generalization.
- **Decision cadence increased** from 180s to 240s. Fewer decisions, fewer
  trades, less commission drag.
- **Horizon extended** from 20 to 30 decisions (120 minutes). A longer horizon
  smooths the label further and may capture slower auction rotations.
- **Ridge lambda increased** from 20.0 to 30.0 for stronger regularization.
- **Trade threshold tightened** from 1% to 0.8% of the prediction distribution,
  keeping only the most confident signals.
- **Rolling window increased** from 900 to 1200 bars (20 minutes) for CVD
  divergence and momentum, capturing longer-term flow.
- **Training data collection changed**: instead of accumulating normal equations
  online, we store (feature, label) pairs and compute standardization and normal
  equations in `fit()`. This allows proper standardization.

All other hyperparameters and the feature set (six features) are identical to #2.
"""

from __future__ import annotations

from collections import deque

import numpy as np

# -- feature set ------------------------------------------------------------- #
N_FEATURES = 6

# -- hyperparameters --------------------------------------------------------- #
INTERVAL = "1s"
DECIDE_EVERY = 240          # changed from 180
HORIZON_STEPS = 30          # changed from 20
WARMUP_BARS = 600
RIDGE_LAMBDA = 30.0         # changed from 20.0
TRADE_FRACTION = 0.008      # changed from 0.01
STOP_POINTS = 5.0
TARGET_POINTS = 15.0
SIZE = 1
TICK_SIZE = 0.25
ROLLING_WINDOW = 1200       # changed from 900
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

        # training data storage (feature, label) pairs
        self.train_pairs: list = []
        self.rows_dropped = 0

        # standardization parameters
        self.feature_mean: np.ndarray | None = None
        self.feature_std: np.ndarray | None = None

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

        # standardize features for prediction
        x_std = (x - self.feature_mean) / self.feature_std
        x_with_bias = np.append(x_std, 1.0)
        predicted = float(np.dot(self.weights, x_with_bias))
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
        self.train_pairs.append((x, y))
        self.pending.popleft()

    def fit(self) -> None:
        if len(self.train_pairs) < 1000:
            raise RuntimeError(
                f"fit() reached with {len(self.train_pairs)} usable rows, which is far "
                f"below what a {HORIZON_STEPS}-step horizon over the fit window "
                f"implies. "
                f"{self.rows_dropped} were dropped at session boundaries. Either "
                "the window is too short or the features are returning None "
                "everywhere -- do not fit a model to this.")

        # Convert to arrays
        X = np.array([pair[0] for pair in self.train_pairs])
        y = np.array([pair[1] for pair in self.train_pairs])

        # Compute standardization parameters
        self.feature_mean = np.mean(X, axis=0)
        self.feature_std = np.std(X, axis=0)
        # Avoid division by zero
        self.feature_std[self.feature_std < 1e-12] = 1.0

        # Standardize features
        X_std = (X - self.feature_mean) / self.feature_std
        # Add bias column
        X_with_bias = np.hstack([X_std, np.ones((X_std.shape[0], 1))])

        # Ridge regression with bias unpenalized
        ridge = RIDGE_LAMBDA * np.eye(N_FEATURES + 1)
        ridge[-1, -1] = 0.0
        self.weights = np.linalg.solve(X_with_bias.T @ X_with_bias + ridge,
                                       X_with_bias.T @ y)

        # Compute predictions on training data for threshold
        preds = np.abs(X_with_bias @ self.weights)
        quantile_edge = float(np.quantile(preds, 1.0 - TRADE_FRACTION))
        self.edge = max(quantile_edge, MIN_EDGE_ABS)

    # -- persistence ---------------------------------------------------------- #
    def save_weights(self) -> bytes:
        if self.weights is None or self.edge is None:
            raise RuntimeError("save_weights() before fit()")
        # Save weights, edge, and standardization parameters
        payload = np.concatenate([
            self.weights,
            [self.edge],
            self.feature_mean,
            self.feature_std,
        ]).astype(np.float64)
        return payload.tobytes()

    def load_weights(self, blob: bytes) -> None:
        data = np.frombuffer(blob, dtype=np.float64)
        expected = N_FEATURES + 1 + 1 + N_FEATURES + N_FEATURES
        if data.size != expected:
            raise ValueError(
                f"weights blob has {data.size} floats, expected {expected}")
        idx = 0
        self.weights = data[idx:idx + N_FEATURES + 1].copy()
        idx += N_FEATURES + 1
        self.edge = float(data[idx])
        idx += 1
        self.feature_mean = data[idx:idx + N_FEATURES].copy()
        idx += N_FEATURES
        self.feature_std = data[idx:idx + N_FEATURES].copy()
