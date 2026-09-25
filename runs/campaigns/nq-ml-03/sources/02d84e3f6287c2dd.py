"""Auction Market Theory features, supervised. Standardized features and adjusted trade frequency.

This variant standardizes the five AMT features before ridge regression, which
can improve conditioning and prediction when feature scales differ. It also
reduces trade fraction to 0.05 and tightens the bracket to 6 points, aiming to
lower commission drag while maintaining enough exposure. Decision cadence and
horizon remain at 2 minutes and 3 decisions (6 minutes).

The core remains: accumulate normal equations for ridge regression on the five
AMT features, use a reservoir to set a quantile threshold, and trade the top
fraction of predictions. Feature standardization is computed from the training
rows and applied at prediction time.
"""

from __future__ import annotations

from collections import deque

import numpy as np

# -- the registered feature set ---------------------------------------------- #
FEATURES = ("dist_vpoc", "va_pos", "at_lvn", "cvd_div", "tape_velocity")
N_FEATURES = 5           # == len(FEATURES); a literal because the agent's static
                         # check (E011) forbids any call at module level

# -- hyperparameters. Each value tried is its own trial under Amendment 4. ---- #
INTERVAL = "1s"          # registered by docs/reviews/task67; ticks are over budget
DECIDE_EVERY = 120       # decision every 2 minutes
HORIZON_STEPS = 3        # label lookahead, in decisions -> 6 minutes
WARMUP_BARS = 600        # per session, before the profile means anything
RIDGE_LAMBDA = 0.5       # regularization strength
TRADE_FRACTION = 0.05    # act on the top 5% of decisions by |prediction| (was 0.10)
RESERVOIR = 8_000        # training rows kept, uniformly at random, to set the edge
STOP_POINTS = 6.0        # tighter bracket to reduce risk per trade
TARGET_POINTS = 6.0
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

        # accumulators for feature standardization
        self.sum_feat = np.zeros(N_FEATURES)
        self.sumsq_feat = np.zeros(N_FEATURES)

        # Reservoir sample of training rows to set the trade threshold.
        self.reservoir: list = []
        self.rows_seen = 0
        self._rng = np.random.default_rng(64_001)

        self.weights: np.ndarray | None = None   # set by fit(), frozen after
        self.edge: float | None = None           # set by fit(), frozen after
        self.feat_mean: np.ndarray | None = None # set by fit()
        self.feat_std: np.ndarray | None = None  # set by fit()

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

        # Standardize the feature vector before prediction
        x_std = (x - self.feat_mean) / self.feat_std
        predicted = float(np.dot(self.weights, np.append(x_std, 1.0)))
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
        """The five AMT primitives, or `None` when they cannot be computed."""
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
        # accumulate feature sums for standardization
        self.sum_feat += x
        self.sumsq_feat += x * x
        self.pending.popleft()

        self.rows_seen += 1
        if len(self.reservoir) < RESERVOIR:
            self.reservoir.append(row)
        else:
            j = int(self._rng.integers(0, self.rows_seen))
            if j < RESERVOIR:
                self.reservoir[j] = row

    def fit(self) -> None:
        """Ridge with standardized features, solved from accumulated normal equations."""
        if self.rows_used < 1000:
            raise RuntimeError(
                f"fit() reached with {self.rows_used} usable rows, which is far "
                f"below what a {HORIZON_STEPS}-step horizon over the fit window "
                f"implies. "
                f"{self.rows_dropped} were dropped at session boundaries. Either "
                "the window is too short or the features are returning None "
                "everywhere -- do not fit a model to this.")

        # compute feature means and standard deviations
        self.feat_mean = self.sum_feat / self.rows_used
        var = self.sumsq_feat / self.rows_used - self.feat_mean**2
        # avoid division by zero
        self.feat_std = np.sqrt(np.maximum(var, 1e-12))

        # transform XtX and Xty to standardized feature space
        # X_std = (X - mean) / std, where X is the raw feature matrix (rows x N_FEATURES)
        # and we append a column of ones for intercept.
        # We need to adjust the normal equations accordingly.
        # Let M = diag(1/std), m = mean vector, and 1 be a column of ones.
        # The raw design matrix is [X, 1].
        # The standardized design matrix is [(X - m) @ M, 1] = [X @ M - m @ M, 1].
        # We can compute new XtX and Xty using the raw ones and the transformation.
        # However, it's easier to rebuild XtX and Xty from scratch using the reservoir
        # if we store all rows, but we don't. We'll use the formula.
        # For simplicity, we'll recompute XtX and Xty from the reservoir sample? That would be biased.
        # Instead, we'll transform using the accumulated raw sums and cross-products.
        # We have:
        # raw_XtX = sum_i [x_i;1] [x_i;1]^T
        # raw_Xty = sum_i [x_i;1] y_i
        # We want:
        # std_XtX = sum_i [ (x_i - m)/s ; 1] [ (x_i - m)/s ; 1]^T
        # std_Xty = sum_i [ (x_i - m)/s ; 1] y_i
        # We can compute these from raw_XtX, raw_Xty, m, s, and the number of rows.
        # Let's denote:
        # S_x = sum_i x_i (raw sum_feat)
        # S_xx = sum_i x_i x_i^T (raw sumsq? Actually XtX top-left block)
        # S_xy = sum_i x_i y_i (raw Xty top block)
        # S_y = sum_i y_i (raw Xty last element)
        # N = rows_used
        # Then:
        # std_XtX top-left block = (raw_XtX[:N_FEATURES,:N_FEATURES] - outer(S_x, m) - outer(m, S_x) + N*outer(m,m)) / (s[:,None]*s[None,:])
        # std_XtX top-right block (features x intercept) = (S_x - N*m) / s
        # std_XtX bottom-left block = transpose of above
        # std_XtX bottom-right = N
        # std_Xty top block = (raw_Xty[:N_FEATURES] - m * S_y) / s
        # std_Xty last = S_y
        # We'll implement this transformation.

        N = self.rows_used
        raw_XtX = self.XtX.copy()
        raw_Xty = self.Xty.copy()

        # extract blocks
        XtX_ff = raw_XtX[:N_FEATURES, :N_FEATURES]  # features x features
        XtX_fi = raw_XtX[:N_FEATURES, N_FEATURES]   # features x intercept
        XtX_if = raw_XtX[N_FEATURES, :N_FEATURES]   # intercept x features
        XtX_ii = raw_XtX[N_FEATURES, N_FEATURES]    # intercept x intercept

        Xty_f = raw_Xty[:N_FEATURES]                # features x y
        Xty_i = raw_Xty[N_FEATURES]                 # intercept x y

        m = self.feat_mean
        s = self.feat_std
        S_x = self.sum_feat
        S_y = Xty_i  # sum of y

        # standardized XtX
        # top-left
        outer_mm = np.outer(m, m)
        outer_Sm = np.outer(S_x, m)
        outer_mS = np.outer(m, S_x)
        XtX_ff_std = (XtX_ff - outer_Sm - outer_mS + N * outer_mm) / (s[:, None] * s[None, :])
        # top-right
        XtX_fi_std = (S_x - N * m) / s
        # bottom-left
        XtX_if_std = XtX_fi_std.copy()
        # bottom-right unchanged
        XtX_ii_std = XtX_ii  # should be N

        # standardized Xty
        Xty_f_std = (Xty_f - m * S_y) / s
        Xty_i_std = Xty_i  # unchanged

        # assemble standardized matrices
        XtX_std = np.zeros_like(raw_XtX)
        XtX_std[:N_FEATURES, :N_FEATURES] = XtX_ff_std
        XtX_std[:N_FEATURES, N_FEATURES] = XtX_fi_std
        XtX_std[N_FEATURES, :N_FEATURES] = XtX_if_std
        XtX_std[N_FEATURES, N_FEATURES] = XtX_ii_std

        Xty_std = np.zeros_like(raw_Xty)
        Xty_std[:N_FEATURES] = Xty_f_std
        Xty_std[N_FEATURES] = Xty_i_std

        ridge = RIDGE_LAMBDA * np.eye(N_FEATURES + 1)
        ridge[-1, -1] = 0.0                      # never penalise the intercept
        self.weights = np.linalg.solve(XtX_std + ridge, Xty_std)

        # Use the reservoir (raw features) to set threshold, but we need to standardize them too.
        # We'll standardize the reservoir rows using the computed mean/std.
        reservoir_std = []
        for row in self.reservoir:
            x_raw = row[:N_FEATURES]
            x_std = (x_raw - self.feat_mean) / self.feat_std
            reservoir_std.append(np.append(x_std, 1.0))
        if reservoir_std:
            preds = np.abs(np.asarray(reservoir_std) @ self.weights)
            self.edge = float(np.quantile(preds, 1.0 - TRADE_FRACTION))
        else:
            # fallback if reservoir empty (should not happen)
            self.edge = 0.0

    # -- the parameters, as bytes; the harness owns the filesystem ----------- #
    def save_weights(self) -> bytes:
        if self.weights is None or self.edge is None or self.feat_mean is None or self.feat_std is None:
            raise RuntimeError("save_weights() before fit()")
        # serialize weights, edge, mean, std
        payload = np.concatenate([self.weights, [self.edge], self.feat_mean, self.feat_std])
        return payload.astype(np.float64).tobytes()

    def load_weights(self, blob: bytes) -> None:
        w = np.frombuffer(blob, dtype=np.float64)
        expected_len = N_FEATURES + 1 + 1 + N_FEATURES + N_FEATURES  # weights (6) + edge (1) + mean (5) + std (5) = 17
        if w.size != expected_len:
            raise ValueError(
                f"weights are {w.size} long, expected {expected_len}")
        idx = 0
        self.weights = w[idx:idx+N_FEATURES+1].copy()
        idx += N_FEATURES + 1
        self.edge = float(w[idx])
        idx += 1
        self.feat_mean = w[idx:idx+N_FEATURES].copy()
        idx += N_FEATURES
        self.feat_std = w[idx:idx+N_FEATURES].copy()
