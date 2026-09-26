"""Auction Market Theory features, supervised. The champion for nq-ml-01.

Implements `config/hypotheses/auction-market-features.md`: ViperQ's feature set
(arXiv 2609.13825) with a supervised forward-return target rather than its PPO
agent, for the three reasons that file gives.

**The memory problem, and why this never stores a training row.** The fit window
is 284 sessions of one-second bars -- roughly 14 million rows. Holding feature
vectors to fit at the end would exhaust the child process's memory limit. So the
normal equations are accumulated instead: `XtX += x x'` and `Xty += x y` as each
label becomes available, and `fit()` solves the ridge system from those. Memory is O(HORIZON_STEPS * d) regardless of how long the fit window is.

That also purges the edges for free. A row at the end of the fit window never
gets a label, because the bar that would supply it is in the scored window and
has not been delivered -- so it is never accumulated. Section 2f's rule, obtained
by construction rather than by trimming.
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
DECIDE_EVERY = 60        # one decision a minute; commission dominates at 1s
HORIZON_STEPS = 5        # label lookahead, in decisions -> five minutes
WARMUP_BARS = 600        # per session, before the profile means anything
RIDGE_LAMBDA = 1.0
TRADE_FRACTION = 0.05    # act on the top 5% of decisions by |prediction|
RESERVOIR = 4_000        # training rows kept, uniformly at random, to set the edge
STOP_POINTS = 8.0
TARGET_POINTS = 8.0
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
        self.volume_sum = 0

        # the label buffer: one entry per DECISION, not per bar
        self.pending: deque = deque(maxlen=HORIZON_STEPS + 1)

        # the normal equations, which is all training data ever becomes
        self.XtX = np.zeros((N_FEATURES + 1, N_FEATURES + 1))
        self.Xty = np.zeros(N_FEATURES + 1)
        self.rows_used = 0
        self.rows_dropped = 0

        # **A uniform sample of training rows, to set the trade threshold.** The
        # first version used a fixed 1.5-point edge; the fitted model's
        # predictions never reached it and the smoke traded twice in a hundred
        # sessions. A quantile of |prediction| is scale-free: whatever the
        # model's outputs turn out to be, the strategy acts on the top
        # TRADE_FRACTION of them. Reservoir sampling (Algorithm R) keeps the
        # sample uniform over the whole window in O(RESERVOIR) memory, so the
        # no-stored-rows property above survives.
        self.reservoir: list = []
        self.rows_seen = 0
        self._rng = np.random.default_rng(64_001)

        self.weights: np.ndarray | None = None   # set by fit(), frozen after
        self.edge: float | None = None           # set by fit(), frozen after

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
        self.volume_sum += bar.volume
        self.bars_in_session += 1

        # **Features are computed at the decision cadence, not per bar, and that
        # is a cost decision AND a correctness one.** `value_area()` and
        # `low_volume_nodes()` walk the profile's price levels, ~80 us and ~40 us
        # at a mid-session NQ profile of a few hundred levels. Per 1s bar that is
        # roughly 12 s a session, about 4,600 s over the split, on top of the
        # 1,726 s of delivery Task 67 measured -- against a 7,200 s budget. Once a
        # minute it is negligible.
        #
        # It also aligns training with inference: the model is fitted on the same
        # kind of row it later predicts on, rather than on bars it never sees a
        # decision at.
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

    # -- features ------------------------------------------------------------ #
    def _new_session(self, bar) -> None:
        """A profile carried across sessions is a different feature."""
        self.profile.reset()
        self.session = bar.session
        self.bars_in_session = 0
        self.cum_delta = 0
        self.tick_count_sum = 0
        self.volume_sum = 0
        self.session_open = bar.open

    def _features(self, bar):
        """The five AMT primitives, or `None` when they cannot be computed.

        `None` rather than zero, per section 2f: a feature nobody measured must
        not enter the model as a number somebody made up. Early in a session the
        profile has too little in it to mean anything, so those bars are skipped
        on both sides -- they are neither trained on nor traded on.
        """
        if self.bars_in_session < WARMUP_BARS or self.profile.is_empty:
            return None
        band = self.profile.value_area()
        if band is None:
            return None

        # 1-2. where price sits in the auction, in ticks and as a region.
        dist = self.profile.distance_from_vpoc(bar.close)
        if dist is None:
            return None
        va_pos = self.profile.value_area_position(bar.close)

        # 3. is price at a level the session has traded thinly through?
        nodes = self.profile.low_volume_nodes()
        at_lvn = 1.0 if any(abs(bar.close - n) <= TICK_SIZE for n in nodes) else 0.0

        # 4. CVD divergence: flow and price disagreeing, each scaled by the
        #    session's own history so the number means the same thing on a quiet
        #    morning and on a payrolls release.
        span = max(self.profile.total_volume, 1)
        flow = self.cum_delta / span
        move = (bar.close - self.session_open) / max(abs(self.session_open), 1.0)
        cvd_div = flow - move * 100.0

        # 5. tape velocity against the session's own running rate. The
        #    hypothesis registers this as "trades AND volume per unit time";
        #    this read `bar.tick_count` only and never `bar.volume`, which the
        #    economics committee seat caught. Both halves are normalised by the
        #    session's own running mean and averaged, so the feature keeps its
        #    scale and the code does what the registered wording says. A burst
        #    of many small trades and one of few large ones are different tape,
        #    and only the pair tells them apart.
        bars = max(self.bars_in_session, 1)
        mean_ticks = self.tick_count_sum / bars
        mean_volume = self.volume_sum / bars
        v_trades = (bar.tick_count / mean_ticks - 1.0) if mean_ticks > 0 else 0.0
        v_volume = (bar.volume / mean_volume - 1.0) if mean_volume > 0 else 0.0
        velocity = 0.5 * (v_trades + v_volume)

        return np.array([dist / 100.0, float(va_pos), at_lvn, cvd_div, velocity])

    # -- training, without ever holding a training set ----------------------- #
    def _retire_labels(self, bar) -> None:
        """Turn the oldest pending row into a label, if its future has arrived.

        Refused across a session boundary: a five-minute forward return that
        spans an overnight gap is not the quantity this is trying to predict.
        """
        # **`<`, not `<=`.** Retirement runs BEFORE the append, so a row put in
        # at decision k is the head once the deque holds HORIZON_STEPS entries,
        # at decision k + HORIZON_STEPS. The old `<=` waited one decision longer
        # and labelled at k + HORIZON_STEPS + 1 -- a 6-minute forward return
        # where this file says 5, and 15 minutes where nq-ml-03's champion said
        # 10. Found by the economics committee seat reading the source.
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

        # Algorithm R: every row has probability RESERVOIR / rows_seen of being
        # in the sample, whatever the window length.
        self.rows_seen += 1
        if len(self.reservoir) < RESERVOIR:
            self.reservoir.append(row)
        else:
            j = int(self._rng.integers(0, self.rows_seen))
            if j < RESERVOIR:
                self.reservoir[j] = row

    def fit(self) -> None:
        """Ridge, solved from the accumulated normal equations. Called once.

        The assertion is section 2f's: if the usable row count is far from what
        the window implies, something filled missing values rather than dropping
        them, and the model would be learning from numbers nobody measured.
        """
        if self.rows_used < 1000:
            raise RuntimeError(
                f"fit() reached with {self.rows_used} usable rows, which is far "
                f"below what a {HORIZON_STEPS}-step horizon over the fit window "
                f"implies. "
                f"{self.rows_dropped} were dropped at session boundaries. Either "
                "the window is too short or the features are returning None "
                "everywhere -- do not fit a model to this.")
        ridge = RIDGE_LAMBDA * np.eye(N_FEATURES + 1)
        ridge[-1, -1] = 0.0                      # never penalise the intercept
        self.weights = np.linalg.solve(self.XtX + ridge, self.Xty)

        # The edge is the (1 - TRADE_FRACTION) quantile of |prediction| over the
        # reservoir: a threshold in the model's own units, chosen on the fit
        # window and frozen with the weights. Not a cost floor -- whether the
        # trades it admits survive commission is what the score is for.
        preds = np.abs(np.asarray(self.reservoir) @ self.weights)
        self.edge = float(np.quantile(preds, 1.0 - TRADE_FRACTION))

    # -- the parameters, as bytes; the harness owns the filesystem ----------- #
    def save_weights(self) -> bytes:
        """Coefficients, intercept, then the edge -- the edge is a fitted
        parameter and travels with the rest."""
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
