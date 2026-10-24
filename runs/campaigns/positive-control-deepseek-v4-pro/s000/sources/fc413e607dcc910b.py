"""Improved fitted early-session direction: fill-realistic evaluation, cost-aware flips, fewer whipsaws.

Builds on the best prior shape (trial #34): one low-turnover +1/-1 position
held nearly continuously, with a single decision per session after the fitted
bar `k`.  This version changes the fitting so it mirrors the engine's T+1
fill: the entry is modelled at `sb[k].open`, not at the signal bar's close.
It also subtracts a per-flip penalty inside the objective so the optimizer
prefers schedules that survive commission, and it uses a higher threshold
quantile for runtime flips (0.50 rather than 0.25) to reduce marginal
whipsaw turnover, which prior trials show is expensive.

Runtime remains simple: each session, at bar k, compute early return from
the session open to the current close and choose direction by the fitted
sign.  Initial entries are always taken; flips require the early return to
be strong by the fitted threshold.  Nothing is kept in order ids; market
orders are found fresh each time.
"""

from __future__ import annotations


class Strategy:
    """Fitted early-session direction with fill-realistic and cost-aware fitting."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 1,
        max_lookback: int = 8,
        flip_threshold_quantile: float = 0.50,
        flip_cost_frac: float = 0.50,
        min_samples: int = 15,
    ) -> None:
        self.interval = interval
        self.size = size
        self.max_lookback = max_lookback
        self.flip_threshold_quantile = flip_threshold_quantile
        self.flip_cost_frac = flip_cost_frac
        self.min_samples = min_samples

        # Fitted in setup()
        self._lookback = 1
        self._sign = 1
        self._drift_direction = 1
        self._flip_tau = 0.0

        # Runtime session state
        self._session: int | None = None
        self._bars_this_session = 0
        self._first_open: float | None = None
        self._evaluated_this_session = False

    # ------------------------------------------------------------------
    # Setup / fitting
    # ------------------------------------------------------------------
    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        self._fit(ctx)

    def _fit(self, ctx) -> None:
        """Fit lookback, sign, and flip threshold from training bars."""
        try:
            bars = tuple(ctx.train_bars(self.interval))
        except Exception:
            bars = ()

        if bars:
            self._drift_direction = 1 if bars[-1].close >= bars[0].close else -1
        else:
            self._drift_direction = 1

        sessions: dict[int, list] = {}
        for bar in bars:
            sessions.setdefault(bar.session, []).append(bar)
        sorted_sessions = sorted(sessions.keys())

        best_key = None
        best_k = 1
        best_sign = 1
        best_rows = None

        for k in range(1, self.max_lookback + 1):
            rows = []
            for sid in sorted_sessions:
                sb = sessions[sid]
                if len(sb) < k + 2:
                    continue
                first_open = sb[0].open
                signal_close = sb[k - 1].close
                fill_price = sb[k].open  # T+1: order queued on signal bar close
                early_ret = signal_close - first_open
                if abs(early_ret) < 1e-12:
                    continue
                rows.append((sid, early_ret, fill_price))

            if len(rows) < self.min_samples:
                continue

            fill_prices = [r[2] for r in rows]
            returns = [
                fill_prices[i + 1] - fill_prices[i]
                for i in range(len(fill_prices) - 1)
            ]
            returns.append(bars[-1].close - fill_prices[-1])

            median_abs_return = self._median_abs(returns)
            if median_abs_return <= 0.0:
                median_abs_return = 1.0
            flip_cost = self.flip_cost_frac * median_abs_return

            for sign in (1, -1):
                desired = [1 if sign * r[1] > 0 else -1 for r in rows]
                raw = sum(desired[i] * returns[i] for i in range(len(returns)))

                flips = sum(
                    1 for i in range(1, len(desired))
                    if desired[i] != desired[i - 1]
                )
                net = raw - flips * flip_cost

                # Prefer higher net, then fewer flips, then higher raw P&L.
                key = (net, -flips, raw)
                if best_key is None or key > best_key:
                    best_key = key
                    best_k = k
                    best_sign = sign
                    best_rows = rows

        if best_rows is not None:
            self._lookback = best_k
            self._sign = best_sign
            self._flip_tau = self._quantile_abs_returns(
                best_rows, self.flip_threshold_quantile
            )
        else:
            self._lookback = 1
            self._sign = 1
            self._flip_tau = 0.0

    @staticmethod
    def _median_abs(values: list[float]) -> float:
        if not values:
            return 0.0
        ordered = sorted(abs(v) for v in values)
        n = len(ordered)
        if n % 2:
            return ordered[n // 2]
        return (ordered[n // 2 - 1] + ordered[n // 2]) / 2.0

    def _quantile_abs_returns(self, rows, q: float) -> float:
        values = sorted(abs(r[1]) for r in rows)
        if not values:
            return 0.0
        idx = int(q * len(values))
        if idx >= len(values):
            idx = len(values) - 1
        return values[idx]

    # ------------------------------------------------------------------
    # Runtime helpers
    # ------------------------------------------------------------------
    def _direction_for_early_ret(self, early_ret: float) -> int:
        """Map a nonzero early return to the fitted long/short convention."""
        if early_ret > 0:
            return self._sign
        return -self._sign

    def _apply_desired(self, ctx, desired: int) -> None:
        """Queue the single market order needed to move to the desired position."""
        pos = ctx.position

        if desired > 0:
            if pos == 0:
                ctx.buy(size=self.size, tif="DAY", tag="session-long")
            elif pos < 0:
                ctx.buy(size=-pos + self.size, tif="DAY", tag="session-flip-long")
        elif desired < 0:
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

        # Session changed: reset per-session state.
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_this_session = 0
            self._first_open = bar.open
            self._evaluated_this_session = False

        self._bars_this_session += 1

        if not ctx.actionable:
            return
        if ctx.working_order_ids:
            return
        if self._evaluated_this_session or self._bars_this_session < self._lookback:
            return

        self._evaluated_this_session = True

        first_open = self._first_open
        if first_open is None:
            return
        early_ret = bar.close - first_open
        pos = ctx.position

        # Zero early return: if flat, establish drift exposure rather than
        # waiting indefinitely; if already in a position, do nothing.
        if abs(early_ret) < 1e-12:
            if pos == 0:
                self._apply_desired(ctx, self._drift_direction)
            return

        desired = self._direction_for_early_ret(early_ret)
        current = 1 if pos > 0 else -1 if pos < 0 else 0

        if current == 0:
            self._apply_desired(ctx, desired)
        elif desired != current:
            # Only flip on a strong contrary signal. Marginal signals are
            # likely noise and turnover is expensive.
            if abs(early_ret) >= self._flip_tau:
                self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
