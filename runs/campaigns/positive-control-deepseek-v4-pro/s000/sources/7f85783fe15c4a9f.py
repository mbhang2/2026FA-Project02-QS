"""Next strategy: interval-selected, fitted early-session direction with
turnover-aware flip threshold and cooldown.

Builds on prior best (#34/#56): one persistent position, one decision per
session, minimal turnover.  It extends that shape by:

- searching several cheap bar intervals (30s, 1m, 2m, 5m, 10m), not just 5m;
- evaluating every candidate with realistic next-bar-open fill prices and a
  transaction-cost penalty;
- fitting not only the lookback and sign but also the flip threshold quantile
  and a session cooldown.

The runtime stays close to the proven #34 pattern and never reaches into
history, so it avoids WindowTooShort and lookahead issues entirely.
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Low-turnover session direction with interval selection and fitted flips."""

    def __init__(
        self,
        *,
        size: int = 1,
        intervals: tuple[str, ...] = ("30s", "1m", "2m", "5m", "10m"),
        max_lookback: int = 8,
        min_samples: int = 10,
        fee_frac: float = 0.10,
        flip_quantiles: tuple[float, ...] = (
            0.00,
            0.10,
            0.20,
            0.30,
            0.40,
            0.50,
            0.65,
            0.80,
            1.00,
        ),
        cooldown_sessions: tuple[int, ...] = (0, 1, 2, 3, 5, 8),
    ) -> None:
        self.size = size
        self.intervals = intervals
        self.max_lookback = max_lookback
        self.min_samples = min_samples
        self.fee_frac = fee_frac
        self.flip_quantiles = flip_quantiles
        self.cooldown_sessions = cooldown_sessions

        # Fitted or safe defaults before setup() runs.
        self.interval = "5m"
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
        # Subscribe to all candidate intervals up front.  Bar subscriptions are
        # cheap, and this avoids any late-subscribe execution quirks.
        for interval in self.intervals:
            ctx.subscribe(interval)

        self._fit(ctx)

    def _fit(self, ctx) -> None:
        """Fit interval, lookback, sign, flip threshold, and cooldown."""
        # Full-sample drift fallback, used when an early return is exactly flat.
        drift_direction = 1
        try:
            bars5 = tuple(ctx.train_bars("5m"))
            if bars5:
                drift_direction = 1 if bars5[-1].close >= bars5[0].close else -1
        except Exception:
            pass
        self._drift_direction = drift_direction

        best_key: tuple[Any, ...] | None = None
        chosen_interval = "5m"
        chosen_lookback = 1
        chosen_sign = 1
        chosen_tau = 0.0
        chosen_cooldown = 0

        for interval in self.intervals:
            try:
                bars = tuple(ctx.train_bars(interval))
            except Exception:
                continue

            if not bars:
                continue

            sessions = self._group_sessions(bars)
            sorted_sessions = sorted(sessions)
            if len(sorted_sessions) < self.min_samples:
                continue

            min_len = min(len(sessions[sid]) for sid in sorted_sessions)
            max_k = min(self.max_lookback, min_len - 1)
            if max_k < 1:
                continue

            for k in range(1, max_k + 1):
                rows: list[tuple[int, float, float]] = []
                for sid in sorted_sessions:
                    sb = sessions[sid]
                    # Need the next bar's open for the fill approximation.
                    if len(sb) <= k:
                        continue
                    first_open = sb[0].open
                    early_ret = sb[k - 1].close - first_open
                    fill_price = sb[k].open
                    rows.append((sid, early_ret, fill_price))

                if len(rows) < self.min_samples:
                    continue

                final_close = sessions[sorted_sessions[-1]][-1].close
                fill_prices = [r[2] for r in rows] + [final_close]
                med_return = self._median_abs_return(fill_prices)
                price_scale = max(med_return, 1e-12)

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
                                drift_direction,
                            )
                            cost = self.fee_frac * price_scale * turnover
                            score = pnl - cost

                            # Primary: higher fitted net P&L.  Tie-breakers
                            # prefer lower turnover, then lower cooldown, then
                            # earlier lookback.
                            key = (score, -turnover, -cooldown, -k)
                            if best_key is None or key > best_key:
                                best_key = key
                                chosen_interval = interval
                                chosen_lookback = k
                                chosen_sign = sign
                                chosen_tau = tau
                                chosen_cooldown = cooldown

        self.interval = chosen_interval
        self._lookback = chosen_lookback
        self._sign = chosen_sign
        self._flip_tau = chosen_tau
        self._cooldown = chosen_cooldown

    def _group_sessions(self, bars: tuple[Any, ...]) -> dict[int, list[Any]]:
        """Group training bars by session, sorted by bar open time."""
        sessions: dict[int, list[Any]] = {}
        for bar in bars:
            sessions.setdefault(bar.session, []).append(bar)
        for sid in sessions:
            sessions[sid].sort(key=lambda b: b.open_ts)
        return sessions

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
                if (
                    last_action_session is None
                    or sid - last_action_session >= cooldown
                ):
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
        diffs = [
            abs(prices[i + 1] - prices[i])
            for i in range(len(prices) - 1)
        ]
        ordered = sorted(diffs)
        n = len(ordered)
        if n % 2:
            return float(ordered[n // 2])
        return (ordered[n // 2 - 1] + ordered[n // 2]) / 2.0

    # ------------------------------------------------------------------
    # Runtime helpers
    # ------------------------------------------------------------------
    def _apply_desired(self, ctx, desired: int) -> None:
        """Queue the one market order needed to move the position to +1 or -1."""
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
        # We subscribe to several intervals; act only when the chosen one closes.
        if self.interval not in ctx.bars:
            return

        bar = ctx.bars[self.interval]

        # New session: reset per-session counting.
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

        # Flat early return: if already in position, hold; if flat, use the
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
            # A fitted session cooldown suppresses noisy reversal bouts.
            if ctx.session - self._last_action_session >= self._cooldown:
                self._apply_desired(ctx, desired)
                self._last_action_session = ctx.session

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
