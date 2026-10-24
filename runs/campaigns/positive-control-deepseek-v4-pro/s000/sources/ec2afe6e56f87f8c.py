"""Expanded fitted session-direction strategy.

This is a direct descendant of the previous best (#34): one low-turnover
continuous +1/-1 position, decided once per session from an early-session
signal.  It keeps the proven 5-minute path and low turnover, but during setup
it searches a wider family of signals:

- early return: close(bar k) - open(bar 1)
- cumulative order-flow imbalance over the first k bars
- close location within the first k bars' range
- overnight gap: previous session close - current session open

For each signal, lookback, sign, and flip threshold, it evaluates continuous
holding P&L net of a turnover penalty and picks the best on the training split.
Runtime then acts in on_bars only at the selected 5m bar, enters when flat, and
flips only on strong contrary signals.
"""

from __future__ import annotations


class Strategy:
    """Fitted session-level direction with broader signal search."""

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

        # Fitted in setup.  Defaults are safe if training data is unavailable.
        self._lookback = 1
        self._sign = 1
        self._drift_direction = 1
        self._flip_tau = 0.0
        self._signal_kind = 0  # 0=early_return, 1=delta_frac, 2=close_loc, 3=overnight_gap

        # Runtime per-session state.
        self._session: int | None = None
        self._bars_this_session = 0
        self._session_bars: list = []
        self._evaluated_this_session = False
        self._prev_session_last_close: float | None = None

    # ------------------------------------------------------------------
    # Setup / fitting
    # ------------------------------------------------------------------
    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        self._fit(ctx)

    def _fit(self, ctx) -> None:
        """Fit signal kind, lookback, sign and flip threshold from training bars."""
        try:
            bars = tuple(ctx.train_bars(self.interval))
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

            # An order submitted on bar k fills on the next tick, so require at
            # least one bar after the signal bar.  This also keeps decisions away
            # from the session's final flushed bar.
            min_len = min(len(sessions[sid]) for sid in sorted_sessions)
            max_k = min(self.max_lookback, max(1, min_len - 1))

            found = False
            best_score = None
            best_key = None
            best_k = 1
            best_sign = 1
            best_tau = 0.0
            best_kind = 0

            for kind in (0, 1, 2, 3):
                for k in range(1, max_k + 1):
                    rows: list[tuple[int, float, float]] = []
                    for idx, sid in enumerate(sorted_sessions):
                        sb = sessions[sid]
                        if len(sb) < k + 1:
                            continue
                        value = self._signal_value_from_session_bars(
                            kind, sb, k, idx, sorted_sessions, sessions
                        )
                        if abs(value) < 1e-12:
                            continue
                        # Use the signal-bar close as the reference price, exactly
                        # as the prior best did; it is a good approximation of the
                        # engine's next-tick fill.
                        rows.append((sid, value, sb[k - 1].close))

                    if len(rows) < self.min_samples:
                        continue

                    fill_prices = [r[2] for r in rows]
                    med_return = self._median_abs_return(
                        fill_prices + [final_close]
                    )

                    for sign in (1, -1):
                        for q in self.flip_quantiles:
                            tau = self._quantile_abs(rows, q)
                            pnl, trades = self._eval_with_threshold(
                                rows, sign, tau, final_close
                            )
                            cost = (
                                self.fee_frac
                                * med_return
                                * (2 * trades - 1)
                                if trades > 0 and med_return > 0
                                else 0.0
                            )
                            score = pnl - cost

                            # Prefer higher score, then fewer flips, then
                            # smaller lookback.
                            key = (score, -trades, -k)
                            if best_key is None or key > best_key:
                                best_key = key
                                best_score = score
                                best_k = k
                                best_sign = sign
                                best_tau = tau
                                best_kind = kind
                                found = True

            if found:
                self._lookback = best_k
                self._sign = best_sign
                self._flip_tau = best_tau
                self._signal_kind = best_kind
        except Exception:
            # Bulk fitting is a convenience, not a right.  Runtime falls back
            # to the simple default if anything goes wrong.
            pass

    def _signal_value_from_session_bars(
        self,
        kind: int,
        sb: list,
        k: int,
        session_idx: int,
        sorted_sessions: list[int],
        sessions: dict[int, list],
    ) -> float:
        """Compute the signal value for a given training session and lookback."""
        if kind == 0:
            return sb[k - 1].close - sb[0].open

        if kind == 1:
            window = sb[:k]
            delta_sum = sum(self._bar_delta(b) for b in window)
            volume_sum = sum(b.volume for b in window)
            if volume_sum <= 1e-12:
                return 0.0
            return delta_sum / volume_sum

        if kind == 2:
            window = sb[:k]
            high = max(b.high for b in window)
            low = min(b.low for b in window)
            if high - low <= 1e-12:
                return 0.0
            return 2.0 * (sb[k - 1].close - low) / (high - low) - 1.0

        # kind == 3: overnight gap, previous session close to current open.
        if session_idx == 0:
            return 0.0
        prev_sid = sorted_sessions[session_idx - 1]
        prev_close = sessions[prev_sid][-1].close
        return prev_close - sb[0].open

    @staticmethod
    def _bar_delta(bar) -> float:
        """Return signed order flow for a bar, with a safe fallback."""
        try:
            return float(bar.delta)
        except Exception:
            return float(getattr(bar, "buy_volume", 0)) - float(
                getattr(bar, "sell_volume", 0)
            )

    def _eval_with_threshold(
        self,
        rows: list[tuple[int, float, float]],
        sign: int,
        tau: float,
        final_close: float,
    ) -> tuple[float, int]:
        """Evaluate continuous holding P&L for a fixed sign and flip threshold."""
        pnl = 0.0
        prev_entry: float | None = None
        prev_pos = 0
        trades = 0

        for _sid, value, entry_close in rows:
            if prev_entry is not None:
                pnl += (entry_close - prev_entry) * prev_pos

            desired = 1 if sign * value > 0 else -1

            if prev_pos == 0:
                new_pos = desired
                trades += 1
            elif desired != prev_pos:
                if abs(value) >= tau:
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
    def _compute_runtime_signal(self, bar) -> float:
        """Compute the selected signal from current session state."""
        k = self._lookback
        window = self._session_bars[:k]
        if not window:
            return 0.0

        if self._signal_kind == 0:
            return bar.close - self._session_bars[0].open

        if self._signal_kind == 1:
            delta_sum = sum(self._bar_delta(b) for b in window)
            volume_sum = sum(b.volume for b in window)
            if volume_sum <= 1e-12:
                return 0.0
            return delta_sum / volume_sum

        if self._signal_kind == 2:
            high = max(b.high for b in window)
            low = min(b.low for b in window)
            if high - low <= 1e-12:
                return 0.0
            return 2.0 * (bar.close - low) / (high - low) - 1.0

        # self._signal_kind == 3: overnight gap.
        if self._prev_session_last_close is None:
            return 0.0
        return self._prev_session_last_close - self._session_bars[0].open

    def _apply_desired(self, ctx, desired: int) -> None:
        """Queue the single market order needed to move to +1 or -1."""
        if desired == 0:
            return

        pos = ctx.position
        if desired > 0:
            if pos == 0:
                ctx.buy(size=self.size, tif="DAY", tag="session-long")
            elif pos < 0:
                ctx.buy(
                    size=-pos + self.size,
                    tif="DAY",
                    tag="session-flip-long",
                )
        else:
            if pos == 0:
                ctx.sell(size=self.size, tif="DAY", tag="session-short")
            elif pos > 0:
                ctx.sell(
                    size=pos + self.size,
                    tif="DAY",
                    tag="session-flip-short",
                )

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------
    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return

        bar = ctx.bars[self.interval]

        # Session changed: preserve the previous session's last close for the
        # overnight-gap signal, then reset per-session state.
        if ctx.session != self._session:
            if self._session is not None and self._session_bars:
                self._prev_session_last_close = self._session_bars[-1].close
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

        value = self._compute_runtime_signal(bar)
        pos = ctx.position

        # No signal: if already in a position, do nothing; if flat, use the
        # full-sample drift direction so exposure is still established.
        if abs(value) < 1e-12:
            if pos == 0:
                self._apply_desired(ctx, self._drift_direction)
            return

        desired = 1 if self._sign * value > 0 else -1
        current = 1 if pos > 0 else -1 if pos < 0 else 0

        if current == 0:
            self._apply_desired(ctx, desired)
        elif desired != current and abs(value) >= self._flip_tau:
            self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
