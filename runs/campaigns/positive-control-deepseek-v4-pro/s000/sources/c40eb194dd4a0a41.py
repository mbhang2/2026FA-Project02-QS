"""Fitted early-session direction with a cooldown on reversals.

The strongest prior strategies in this campaign (#34, #54, #55) are
one-decision-per-session directional strategies.  They fit a lookback k and a
sign from training bars so that, on bar k of each session,

    early_ret = close(bar k) - open(bar 1)

selects a desired position +1 or -1.  Runtime then holds that position almost
continuously and flips only on a strong contrary signal.

This proposal keeps that entire shape but adds a fitted **cooldown in
sessions**: after an entry or flip, an opposite signal is ignored for at
least `cooldown_sessions` sessions even if its magnitude exceeds the flip
threshold.  A cooldown is the natural next lever after the flip threshold,
because the prior campaign already demonstrated that reducing turnover from 48
trades (#33) to 38 trades (#34) improved the score.  Fitting it on the
training split lets the data decide whether fewer reversals are better, while
still forcing exposure to be established on the first signal so the strategy
does not fall below the exposure floor.

Training evaluation uses the realistic fill assumption from the best recent
variant: a market order submitted at a bar close is eligible from the next
tick, which on bar alignment is approximated by the next bar's open.
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Fitted early-session direction, held with minimal turnover and fitted cooldown."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 1,
        max_lookback: int = 16,
        min_samples: int = 10,
        fee_frac: float = 0.05,
        flip_quantiles: tuple[float, ...] = (
            0.10,
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
        cooldown_sessions: tuple[int, ...] = (0, 1, 2, 3),
    ) -> None:
        self.interval = interval
        self.size = size
        self.max_lookback = max_lookback
        self.min_samples = min_samples
        self.fee_frac = fee_frac
        self.flip_quantiles = flip_quantiles
        self.cooldown_sessions = cooldown_sessions

        # Fitted or safe defaults before setup() runs.
        self._lookback = 1
        self._sign = 1
        self._drift_direction = 1
        self._flip_tau = 0.0
        self._cooldown = 0

        # Runtime per-session state.
        self._session: int | None = None
        self._bars_this_session = 0
        self._session_bars: list[Any] = []
        self._evaluated_this_session = False
        self._last_action_session = -10_000_000

    # ------------------------------------------------------------------
    # Setup / fitting
    # ------------------------------------------------------------------
    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        self._fit(ctx)

    def _fit(self, ctx) -> None:
        """Fit lookback, sign, flip threshold, and cooldown from bulk training bars."""
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

            # A signal on bar k-1 needs a following bar k for the next-tick
            # fill approximation, so every session used needs at least k+1 bars.
            min_len = min(len(sessions[sid]) for sid in sorted_sessions)
            if min_len < 3:
                return

            max_k = min(self.max_lookback, min_len - 1)

            found = False
            best_key = None

            for k in range(1, max_k + 1):
                rows: list[tuple[int, float, float]] = []
                for sid in sorted_sessions:
                    sb = sessions[sid]
                    if len(sb) < k + 1:
                        continue
                    signal_bar = sb[k - 1]
                    first_open = sb[0].open
                    early_ret = signal_bar.close - first_open
                    # Market order submitted at this bar's close is eligible
                    # from the next tick, approximated by the next bar's open.
                    fill_price = sb[k].open
                    rows.append((sid, early_ret, fill_price))

                if len(rows) < self.min_samples:
                    continue

                fill_prices = [r[2] for r in rows] + [final_close]
                med_return = self._median_abs_return(fill_prices)

                abs_values = [abs(r[1]) for r in rows]
                if not abs_values:
                    abs_values = [0.0]

                for sign in (1, -1):
                    for q in self.flip_quantiles:
                        tau = self._quantile_abs(abs_values, q)
                        for cooldown in self.cooldown_sessions:
                            pnl, turnover = self._eval_rows(
                                rows,
                                sign,
                                tau,
                                cooldown,
                                final_close,
                                self._drift_direction,
                            )
                            cost = self.fee_frac * med_return * turnover
                            score = pnl - cost

                            # Prefer higher score, then lower turnover, then
                            # smaller lookback, then smaller cooldown.
                            key = (score, -turnover, -k, -cooldown)
                            if best_key is None or key > best_key:
                                best_key = key
                                found = True
                                self._lookback = k
                                self._sign = sign
                                self._flip_tau = tau
                                self._cooldown = cooldown

            if not found:
                # Defaults remain: 5m, lookback 1, sign 1, no cooldown.
                pass
        except Exception:
            # Bulk fitting is a convenience, not a right.  Runtime falls back
            # to the simple default if anything goes wrong.
            pass

    def _eval_rows(
        self,
        rows: list[tuple[int, float, float]],
        sign: int,
        tau: float,
        cooldown: int,
        final_close: float,
        drift_direction: int,
    ) -> tuple[float, float]:
        """Evaluate continuous holding P&L and turnover for one configuration."""
        pnl = 0.0
        turnover = 0.0
        prev_pos = 0
        prev_price: float | None = None
        last_action_session: int | None = None

        for sid, value, price in rows:
            if prev_price is not None:
                pnl += (price - prev_price) * prev_pos

            if value > 1e-12:
                desired = sign
            elif value < -1e-12:
                desired = -sign
            else:
                # Flat early return: stay if already in position; if flat,
                # establish exposure in the full-sample drift direction.
                desired = prev_pos if prev_pos != 0 else drift_direction

            new_pos = prev_pos
            if prev_pos == 0:
                if desired != 0:
                    new_pos = desired
                    last_action_session = sid
            elif desired != prev_pos and abs(value) >= tau:
                if last_action_session is None or sid - last_action_session >= cooldown:
                    new_pos = desired
                    last_action_session = sid

            if new_pos != prev_pos:
                turnover += abs(new_pos - prev_pos)

            prev_pos = new_pos
            prev_price = price

        if prev_price is not None:
            pnl += (final_close - prev_price) * prev_pos

        return pnl, turnover

    @staticmethod
    def _quantile_abs(values: list[float], q: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = int(q * len(ordered))
        if idx >= len(ordered):
            idx = len(ordered) - 1
        return float(ordered[idx])

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

        # New session: reset per-session state but keep cooldown/position state.
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

        # Flat early return: if already in a position, hold; if flat, use the
        # full-sample drift direction so exposure is still established.
        if abs(early_ret) < 1e-12:
            if pos == 0:
                self._apply_desired(ctx, self._drift_direction)
                self._last_action_session = ctx.session
            return

        desired = 1 if self._sign * early_ret > 0 else -1
        current = 1 if pos > 0 else -1 if pos < 0 else 0

        if current == 0:
            self._apply_desired(ctx, desired)
            self._last_action_session = ctx.session
        elif desired != current and abs(early_ret) >= self._flip_tau:
            # The fitted cooldown suppresses tight bouts of reversing trades.
            if ctx.session - self._last_action_session >= self._cooldown:
                self._apply_desired(ctx, desired)
                self._last_action_session = ctx.session

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
