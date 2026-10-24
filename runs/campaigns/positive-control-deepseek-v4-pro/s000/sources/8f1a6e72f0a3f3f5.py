"""A fitted session-level directional strategy with realistic next-bar-open fills.

Direct descendant of the best prior shape (#34). It keeps the proven 5-minute
single-decision-per-session structure, but changes the training evaluation to
fill simulated orders at the next bar's open instead of the signal bar's close.
The market order submitted at a 5m close is eligible from the next tick, which
on time-bar alignment is the next bar's opening tick. Fitting with this fill
assumption selects lookback, sign, and flip threshold under the same slippage
regime the live walk actually sees, rather than one that is consistently
optimistic by the close-to-open gap.

Runtime remains simple and low turnover: on the selected 5m bar of each
session, compute early_ret = close(bar k) - open(bar 1), enter if flat, and
flip only when a contrary signal is strong. Flat early returns fall back to the
full-sample drift direction so exposure is established.
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Fitted early-session direction, with realistic fill assumption."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 1,
        max_lookback: int = 16,
        min_samples: int = 10,
        fee_frac: float = 0.10,
        flip_quantiles: tuple[float, ...] = (
            0.15,
            0.20,
            0.25,
            0.30,
            0.40,
            0.55,
            0.70,
            0.85,
            1.0,
        ),
    ) -> None:
        self.interval = interval
        self.size = size
        self.max_lookback = max_lookback
        self.min_samples = min_samples
        self.fee_frac = fee_frac
        self.flip_quantiles = flip_quantiles

        # Fitted in setup(); safe defaults if training data is unavailable.
        self._lookback = 1
        self._sign = 1
        self._drift_direction = 1
        self._flip_tau = 0.0

        # Runtime per-session state.
        self._session: int | None = None
        self._bars_this_session = 0
        self._session_bars: list[Any] = []
        self._evaluated_this_session = False

    # ------------------------------------------------------------------
    # Setup / fitting
    # ------------------------------------------------------------------
    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        self._fit(ctx)

    def _fit(self, ctx) -> None:
        """Fit lookback, sign and flip threshold from bulk training bars."""
        try:
            bars = tuple(ctx.train_bars(self.interval))
        except Exception:
            return

        if not bars:
            return

        try:
            sessions: dict[int, list[Any]] = {}
            for bar in bars:
                sessions.setdefault(bar.session, []).append(bar)

            sorted_sessions = sorted(sessions)
            if not sorted_sessions:
                return

            self._drift_direction = 1 if bars[-1].close >= bars[0].close else -1
            final_close = sessions[sorted_sessions[-1]][-1].close

            # A decision on bar k must leave at least one bar whose open can be
            # used as the next-tick fill price in training evaluation.
            min_len = min(len(sessions[sid]) for sid in sorted_sessions)
            max_k = min(self.max_lookback, max(1, min_len - 1))

            found = False
            best_score = -float("inf")
            best_turnover = float("inf")
            best_k = 1
            best_sign = 1
            best_tau = 0.0

            for k in range(1, max_k + 1):
                rows: list[tuple[int, float, float]] = []
                for sid in sorted_sessions:
                    sb = sessions[sid]
                    if len(sb) < k + 1:
                        continue
                    early_ret = sb[k - 1].close - sb[0].open
                    if abs(early_ret) < 1e-12:
                        continue
                    # Fill price for a market order submitted at this bar's close.
                    fill_price = sb[k].open
                    rows.append((sid, early_ret, fill_price))

                if len(rows) < self.min_samples:
                    continue

                fill_prices = [r[2] for r in rows] + [final_close]
                med_return = self._median_abs_return(fill_prices)

                for sign in (1, -1):
                    for q in self.flip_quantiles:
                        tau = self._quantile_abs(rows, q)
                        pnl, turnover = self._eval_rows(rows, sign, tau, final_close)
                        trade_cost = (
                            self.fee_frac * med_return * turnover
                            if turnover > 0 and med_return > 0
                            else 0.0
                        )
                        score = pnl - trade_cost

                        if score > best_score or (
                            score == best_score and turnover < best_turnover
                        ):
                            found = True
                            best_score = score
                            best_turnover = turnover
                            best_k = k
                            best_sign = sign
                            best_tau = tau

            if found:
                self._lookback = best_k
                self._sign = best_sign
                self._flip_tau = best_tau
        except Exception:
            # Bulk fitting is a convenience, not a right. Runtime falls back
            # to the simple default if anything goes wrong.
            pass

    def _eval_rows(
        self,
        rows: list[tuple[int, float, float]],
        sign: int,
        tau: float,
        final_close: float,
    ) -> tuple[float, float]:
        """Evaluate continuous holding P&L using next-bar-open fill prices."""
        pnl = 0.0
        prev_pos = 0
        prev_price: float | None = None
        turnover = 0.0

        for _sid, value, price in rows:
            if prev_price is not None:
                pnl += (price - prev_price) * prev_pos

            desired = 1 if sign * value > 0 else -1
            new_pos = prev_pos

            if prev_pos == 0:
                new_pos = desired
            elif desired != prev_pos and abs(value) >= tau:
                new_pos = desired

            if new_pos != prev_pos:
                turnover += abs(new_pos - prev_pos)

            prev_pos = new_pos
            prev_price = price

        if prev_price is not None:
            pnl += (final_close - prev_price) * prev_pos

        return pnl, turnover

    @staticmethod
    def _quantile_abs(rows: list[tuple[int, float, float]], q: float) -> float:
        values = sorted(abs(r[1]) for r in rows)
        if not values:
            return 0.0
        idx = int(q * len(values))
        if idx >= len(values):
            idx = len(values) - 1
        return values[idx]

    @staticmethod
    def _median_abs_return(prices: list[float]) -> float:
        if len(prices) < 2:
            return 0.0
        diffs = [abs(prices[i + 1] - prices[i]) for i in range(len(prices) - 1)]
        ordered = sorted(diffs)
        n = len(ordered)
        if n % 2:
            return float(ordered[n // 2])
        return (ordered[n // 2 - 1] + ordered[n // 2]) / 2.0

    # ------------------------------------------------------------------
    # Runtime helpers
    # ------------------------------------------------------------------
    def _apply_desired(self, ctx, desired: int) -> None:
        """Queue the single market order needed to move to +1 or -1."""
        if desired == 0:
            return

        pos = ctx.position
        if desired > 0:
            if pos == 0:
                ctx.buy(size=self.size, tif="DAY", tag="session-long")
            elif pos < 0:
                ctx.buy(size=-pos + self.size, tif="DAY", tag="session-flip-long")
        else:
            if pos == 0:
                ctx.sell(size=self.size, tif="DAY", tag="session-short")
            elif pos > 0:
                ctx.sell(size=pos + self.size, tif="DAY", tag="session-flip-short")

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------
    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return

        bar = ctx.bars[self.interval]

        # New session: reset per-session state.
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
        if self._evaluated_this_session:
            return
        if self._bars_this_session < self._lookback:
            return

        self._evaluated_this_session = True

        first_open = self._session_bars[0].open
        early_ret = bar.close - first_open
        pos = ctx.position

        # Flat early return: if already in a position, do nothing; if flat, use
        # the full-sample drift direction so exposure is still established.
        if abs(early_ret) < 1e-12:
            if pos == 0:
                self._apply_desired(ctx, self._drift_direction)
            return

        desired = 1 if self._sign * early_ret > 0 else -1
        current = 1 if pos > 0 else -1 if pos < 0 else 0

        if current == 0:
            self._apply_desired(ctx, desired)
        elif desired != current and abs(early_ret) >= self._flip_tau:
            self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
