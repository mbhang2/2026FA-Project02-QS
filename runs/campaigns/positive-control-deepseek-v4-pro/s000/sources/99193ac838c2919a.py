"""Fitted early-session direction, thresholded entry and flip, size scaled.

Refines the best prior shape in this campaign (#61). The core remains
low-turnover: make one decision per session from the return over the first
lookback bars, hold across sessions, and flip only on a strong contrary signal.
Two changes:

1. A threshold is now applied to *entering* as well as flipping. Small
   early-session moves are more likely noise than signal. The threshold is
   chosen from training as a quantile of absolute early returns, alongside the
   lookback and sign, so a zero threshold remains available if the training
   split says entries should always be taken.
2. Position size is increased from 3 to 4. The edge in the prior campaign
   appeared per-contract and persistent, so the same low-turnover logic should
   scale a little further without adding a new risk surface.

Runtime is intentionally safe: all per-session state is on `self`, action is
taken only when the chosen interval closes, and `ctx.actionable` is checked
before every order.
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Fitted session-direction strategy with thresholded entries and flips."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 4,
        max_lookback: int = 8,
        min_samples: int = 15,
        q_grid: tuple[float, ...] = (0.0, 0.10, 0.20, 0.25, 0.30, 0.40, 0.50),
    ) -> None:
        self.interval = interval
        self.size = size
        self.max_lookback = max_lookback
        self.min_samples = min_samples
        self.q_grid = q_grid

        # Fitted in setup()
        self._lookback = 1
        self._sign = 1
        self._entry_tau = 0.0
        self._flip_tau = 0.0

        # Per-session runtime state
        self._session: int | None = None
        self._bars_this_session = 0
        self._session_bars: list[Any] = []
        self._evaluated_this_session = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        self._fit(ctx)

    def _fit(self, ctx) -> None:
        """Fit lookback, sign, and threshold from training bars."""
        try:
            bars = tuple(ctx.train_bars(self.interval))
        except Exception:
            bars = ()

        if not bars:
            self._lookback = 1
            self._sign = 1
            self._entry_tau = 0.0
            self._flip_tau = 0.0
            return

        sessions: dict[int, list[Any]] = {}
        for bar in bars:
            sessions.setdefault(bar.session, []).append(bar)

        ordered = sorted(sessions)
        min_bars = min(len(sessions[s]) for s in ordered)
        max_k = min(self.max_lookback, min_bars)
        final_close = float(bars[-1].close)

        best_score: float | None = None
        best_k = 1
        best_sign = 1
        best_tau = 0.0

        for k in range(1, max_k + 1):
            rows: list[tuple[float, float]] = []
            for sid in ordered:
                sb = sessions[sid]
                if len(sb) < k:
                    continue
                first_open = float(sb[0].open)
                close_k = float(sb[k - 1].close)
                early_ret = close_k - first_open
                # Zero-return sessions are kept in the threshold distribution,
                # but in simulation they produce no desired side.
                rows.append((early_ret, close_k))

            if len(rows) < self.min_samples:
                continue

            for q in self.q_grid:
                tau = self._quantile_abs(rows, q)
                for sign in (1, -1):
                    score = self._simulate_trial(
                        rows, sign, tau, tau, final_close
                    )
                    if best_score is None or score > best_score:
                        best_score = score
                        best_k = k
                        best_sign = sign
                        best_tau = tau

        self._lookback = best_k
        self._sign = best_sign
        self._entry_tau = best_tau
        self._flip_tau = best_tau

    def _simulate_trial(
        self,
        rows: list[tuple[float, float]],
        sign: int,
        entry_tau: float,
        flip_tau: float,
        final_close: float,
    ) -> float:
        """Continuous holding P&L for a fixed sign/threshold rule."""
        state = 0  # -1 short, 0 flat, +1 long
        entry_price = 0.0
        pnl = 0.0

        for early_ret, price in rows:
            # Mark previous position to the current decision price.
            if state != 0:
                pnl += (price - entry_price) * state

            desired = 0
            if early_ret > 1e-12:
                desired = sign
            elif early_ret < -1e-12:
                desired = -sign

            if state == 0:
                if desired != 0 and abs(early_ret) >= entry_tau:
                    state = desired
                    entry_price = price
            else:
                if desired != 0 and desired != state and abs(early_ret) >= flip_tau:
                    state = desired
                    entry_price = price

        if state != 0:
            pnl += (final_close - entry_price) * state

        return pnl

    def _quantile_abs(
        self, rows: list[tuple[float, float]], q: float
    ) -> float:
        if q <= 0.0:
            return 0.0
        values = sorted(abs(r[0]) for r in rows)
        if not values:
            return 0.0
        idx = int(q * len(values))
        if idx >= len(values):
            idx = len(values) - 1
        return float(values[idx])

    def _desired(self, early_ret: float) -> int:
        if early_ret > 1e-12:
            return self._sign
        if early_ret < -1e-12:
            return -self._sign
        return 0

    def _apply_desired(self, ctx, desired: int) -> None:
        """Queue the market order needed to move from current position to desired."""
        pos = ctx.position
        size = self.size

        if desired > 0:
            if pos == 0:
                ctx.buy(size=size, tif="DAY", tag="session-long")
            elif pos < 0:
                ctx.buy(size=-pos + size, tif="DAY", tag="session-flip-long")
        elif desired < 0:
            if pos == 0:
                ctx.sell(size=size, tif="DAY", tag="session-short")
            elif pos > 0:
                ctx.sell(size=pos + size, tif="DAY", tag="session-flip-short")

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return

        bar = ctx.bars[self.interval]

        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_this_session = 0
            self._session_bars = []
            self._evaluated_this_session = False

        self._bars_this_session += 1
        self._session_bars.append(bar)

        if not ctx.actionable:
            return
        if ctx.working_order_ids:
            return
        if self._evaluated_this_session or self._bars_this_session != self._lookback:
            return

        self._evaluated_this_session = True

        first_open = float(self._session_bars[0].open)
        early_ret = float(bar.close) - first_open
        pos = ctx.position
        current = 1 if pos > 0 else -1 if pos < 0 else 0
        desired = self._desired(early_ret)

        if current == 0:
            # Enter only on a strong enough signal.
            if desired != 0 and abs(early_ret) >= self._entry_tau:
                self._apply_desired(ctx, desired)
            return

        # Already positioned: flip only on a strong opposite signal.
        if desired != 0 and desired != current and abs(early_ret) >= self._flip_tau:
            self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
