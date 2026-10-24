"""Refined session-level directional strategy, fitted in setup and traded in on_bars.

Direct descendant of the best prior shape (#34): one low-turnover continuous
+1/-1 position decided once per session from the early-session return. This
version keeps the proven 5-minute decision path but makes the flip threshold
part of the fitted model rather than a fixed 0.25 quantile. During setup it
searches a small set of possible flip quantiles and selects the combination of
lookback, sign and flip threshold that maximises continuous holding P&L net of
a turnover penalty. The runtime then acts only in on_bars, at the same
5-minute close that has consistently produced fills in this harness.
"""

from __future__ import annotations


class Strategy:
    """Fitted early-session direction with turnover-regularised flips."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 1,
        max_lookback: int = 8,
        min_samples: int = 10,
        fee_frac: float = 0.10,
        flip_quantiles: tuple[float, ...] = (0.25, 0.40, 0.55, 0.70, 0.85, 1.0),
    ) -> None:
        self._interval = interval
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
        self._session_bars: list = []
        self._evaluated_this_session = False

    # ------------------------------------------------------------------
    # Setup / fitting
    # ------------------------------------------------------------------
    def setup(self, ctx) -> None:
        self._fit(ctx)
        ctx.subscribe(self._interval)

    def _fit(self, ctx) -> None:
        """Fit lookback, sign and flip threshold from bulk training bars."""
        try:
            bars = tuple(ctx.train_bars(self._interval))
        except Exception:
            return

        if not bars:
            return

        try:
            sessions: dict[int, list] = {}
            for bar in bars:
                sessions.setdefault(bar.session, []).append(bar)

            sorted_sessions = sorted(sessions)
            if not sorted_sessions:
                return

            self._drift_direction = 1 if bars[-1].close >= bars[0].close else -1
            final_close = sessions[sorted_sessions[-1]][-1].close

            # A decision on bar k must not be on the session's final flushed bar,
            # because an order submitted there can expire before filling.
            min_len = min(len(sessions[sid]) for sid in sorted_sessions)
            max_k = min(self.max_lookback, max(1, min_len - 1))

            found = False
            best_score = 0.0
            best_trades = 10**9
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
                    rows.append((sid, early_ret, sb[k - 1].close))

                if len(rows) < self.min_samples:
                    continue

                for sign in (1, -1):
                    for q in self.flip_quantiles:
                        tau = self._quantile_abs(rows, q)
                        pnl, trades = self._eval_with_threshold(
                            rows, sign, tau, self._drift_direction, final_close
                        )
                        med = self._median_abs_returns(rows, final_close)
                        trade_cost = (
                            self.fee_frac * med * (2 * trades - 1)
                            if trades > 0
                            else 0.0
                        )
                        score = pnl - trade_cost

                        if (
                            not found
                            or score > best_score
                            or (score == best_score and trades < best_trades)
                        ):
                            found = True
                            best_score = score
                            best_trades = trades
                            best_k = k
                            best_sign = sign
                            best_tau = tau

            if found:
                self._lookback = best_k
                self._sign = best_sign
                self._flip_tau = best_tau
        except Exception:
            # Training data is a convenience, not a right. If anything in bulk
            # fitting fails, the runtime falls back to the simple default.
            pass

    def _eval_with_threshold(
        self,
        rows: list[tuple[int, float, float]],
        sign: int,
        tau: float,
        drift_direction: int,
        final_close: float,
    ) -> tuple[float, int]:
        """Evaluate continuous holding P&L for a fixed sign and flip threshold."""
        pnl = 0.0
        prev_entry: float | None = None
        prev_pos = 0
        trades = 0

        for _sid, early_ret, entry_close in rows:
            if prev_entry is not None:
                pnl += (entry_close - prev_entry) * prev_pos

            desired = 1 if sign * early_ret > 0 else -1

            if prev_pos == 0:
                new_pos = desired
                trades += 1
            elif desired != prev_pos:
                if abs(early_ret) >= tau:
                    new_pos = desired
                    trades += 1
                else:
                    new_pos = prev_pos
            else:
                new_pos = prev_pos

            prev_pos = new_pos
            prev_entry = entry_close

        if prev_entry is not None:
            pnl += (final_close - prev_entry) * prev_pos

        return pnl, trades

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
    def _median_abs_returns(
        rows: list[tuple[int, float, float]], final_close: float
    ) -> float:
        prices = [r[2] for r in rows] + [final_close]
        diffs = [prices[i + 1] - prices[i] for i in range(len(prices) - 1)]
        values = sorted(abs(d) for d in diffs)
        if not values:
            return 0.0
        n = len(values)
        if n % 2:
            return float(values[n // 2])
        return (values[n // 2 - 1] + values[n // 2]) / 2.0

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
        if self._interval not in ctx.bars:
            return

        bar = ctx.bars[self._interval]

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

        # Flat early return: if we already hold, do nothing; if flat, use the
        # full-sample drift direction so exposure is still established.
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
