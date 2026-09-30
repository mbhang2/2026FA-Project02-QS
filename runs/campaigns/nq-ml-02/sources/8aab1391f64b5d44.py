"""Auction Market Theory features, supervised. Tenth variation.

Changes from the best variation (#2, score +1.7807):

- **Horizon extended** from 20 to 25 decision intervals (75 minutes). A longer
  horizon may capture slower auction rotations and reduce noise from short-term
  fluctuations.
- **Decision cadence increased** from 180s to 240s (4 minutes). Fewer decisions
  mean fewer trades and less commission drag, while the signal should persist.
- **Rolling window increased** from 900 to 1200 bars (20 minutes) for CVD
  divergence and momentum, capturing longer-term flow and price trends.
- **Bracket asymmetry adjusted** from 5/15 to 4/18 (stop 4, target 18). This
  gives winners more room while limiting losses, with a higher reward-to-risk
  ratio (4.5 vs 3.0).
- **Ridge lambda decreased** from 20.0 to 18.0. Slightly less regularization to
  allow the model to fit the longer horizon signal more flexibly, while still
  guarding against overfitting.
- **Trade threshold widened** from 1% to 1.2% of the reservoir distribution,
  and the absolute floor raised from 0.3 to 0.4 points. This should reduce the
  number of trades further, keeping only the most confident predictions.
- **All other hyperparameters unchanged** from #2: feature set, ridge
  regression, reservoir sampling, memory strategy, 1-second bars, warmup 600.

Everything else (feature set, ridge regression, reservoir sampling, memory
strategy) is identical to #2.
"""

from __future__ import annotations

from collections import deque

import numpy as np

# -- feature set ------------------------------------------------------------- #
# Original five plus momentum.
N_FEATURES = 6

# -- hyperparameters --------------------------------------------------------- #
INTERVAL = "1s"
DECIDE_EVERY = 240          # changed from 180
HORIZON_STEPS = 25          # changed from 20
WARMUP_BARS = 600
RIDGE_LAMBDA = 18.0         # changed from 20.0
TRADE_FRACTION = 0.012      # changed from 0.01
RESERVOIR = 4_000
STOP_POINTS = 4.0           # changed from 5.0
TARGET_POINTS = 18.0        # changed from 15.0
SIZE = 1
TICK_SIZE = 0.25
ROLLING_WINDOW = 1200       # changed from 900
MIN_EDGE_ABS = 0.4          # changed from 0.3


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

        predicted = float(np.dot(self.weights, np.append(x, 1.0)))
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

        # tape velocity (unchanged)
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
        quantile_edge = float(np.quantile(preds, 1.0 - TRADE_FRACTION))
        self.edge = max(quantile_edge, MIN_EDGE_ABS)

    # -- persistence ---------------------------------------------------------- #
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
