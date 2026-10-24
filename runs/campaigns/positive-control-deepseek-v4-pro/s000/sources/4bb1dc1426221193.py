"""A fitted, low-turnover session-direction strategy.

The best prior trial (#34) held a near-continuous +1/-1 position and changed it
only on a strong early-session signal. This version keeps that proven shape but
tightens the fit:

- It fits the early-return lookback `k`, the direction convention, and **the
  flip threshold quantile itself** by simulating the same one-decision-per-session
  runtime on the training bars.
- The simulation uses T+1 realistic entry at the next bar's open, and subtracts
  a small per-contract turnover cost so the optimizer prefers schedules that
  survive commission rather than merely maximizing gross P&L.
- At runtime it still enters whenever flat, flips only when the contrary early
  return is strong, and otherwise holds, so exposure is established early and
  turnover stays low.

This is deliberately close to the most robust prior shape: any change that
raises a runtime error or stops trading is a wasted trial.
"""

from __future__ import annotations


class Strategy:
    """Fitted early-session direction with cost-aware threshold search."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 1,
        max_lookback: int = 8,
        min_samples: int = 10,
        cost_frac: float = 0.10,
    ) -> None:
        self.interval = interval
        self.size = size
        self.max_lookback = max_lookback
        self.min_samples = min_samples
        self.cost_frac = cost_frac

        # Fitted in setup()
        self._lookback = 1
        self._sign = 1
        self._flip_tau = 0.0
        self._drift_direction = 1
        self._final_close: float = 0.0

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
        try:
            self._fit(ctx)
        except Exception:
            # If fitting fails for any reason, fall back to a safe and very
            # simple momentum rule -- never to silence.
            self._lookback = 1
            self._sign = 1
            self._flip_tau = 0.0
            self._drift_direction = 1
            self._final_close = 0.0

    def _fit(self, ctx) -> None:
        """Fit lookback, sign, and flip threshold from training bars."""
        try:
            bars = tuple(ctx.train_bars(self.interval))
        except Exception:
            bars = ()

        if not bars:
            self._lookback = 1
            self._sign = 1
            self._flip_tau = 0.0
            self._drift_direction = 1
            self._final_close = 0.0
            return

        self._drift_direction = 1 if bars[-1].close >= bars[0].open else -1
        self._final_close = bars[-1].close

        sessions: dict[int, list] = {}
        for bar in bars:
            sessions.setdefault(bar.session, []).append(bar)
        sorted_sessions = sorted(sessions.keys())

        q_candidates = (0.0, 0.10, 0.20, 0.25, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80)

        best_key = None
        best_lookback = 1
        best_sign = 1
        best_tau = 0.0

        for k in range(1, self.max_lookback + 1):
            rows: list[tuple[float, float]] = []
            fill_prices: list[float] = []

            for sid in sorted_sessions:
                sb = sessions[sid]
                # The signal is bar k-1 (0-based). The order queued on its close
                # is eligible from the next tick, which is the next bar's open.
                if len(sb) < k + 1:
                    continue
                first_open = sb[0].open
                signal_close = sb[k - 1].close
                fill_price = sb[k].open
                early_ret = signal_close - first_open
                rows.append((early_ret, fill_price))
                fill_prices.append(fill_price)

            if len(rows) < self.min_samples:
                continue

            med_abs_return = self._median_abs(
                [fill_prices[i + 1] - fill_prices[i] for i in range(len(fill_prices) - 1)]
            )
            if med_abs_return <= 0.0:
                med_abs_return = 1.0
            cost_per_contract = self.cost_frac * med_abs_return

            for sign in (1, -1):
                for q in q_candidates:
                    tau = self._quantile_abs(rows, q)
                    net, trades, flips = self._simulate(
                        rows, sign, tau, self._drift_direction, cost_per_contract
                    )
                    key = (net, -trades, -flips)
                    if best_key is None or key > best_key:
                        best_key = key
                        best_lookback = k
                        best_sign = sign
                        best_tau = tau

        self._lookback = best_lookback
        self._sign = best_sign
        self._flip_tau = best_tau

    def _simulate(
        self,
        rows: list[tuple[float, float]],
        sign: int,
        tau: float,
        drift_direction: int,
        cost_per_contract: float,
    ) -> tuple[float, int, int]:
        """Simulate the one-decision-per-session runtime.

        Returns (net_pnl, number_of_trades, number_of_flips). Rows are
        (early_return, fill_price_at_next_bar_open).
        """
        pos = 0
        trade_fills: list[tuple[float, int]] = []
        total_cost = 0.0
        trades = 0
        flips = 0

        for early_ret, fill_price in rows:
            current = pos

            if abs(early_ret) < 1e-12:
                desired = drift_direction if current == 0 else current
            else:
                desired = sign if early_ret > 0 else -sign

            if current == 0:
                trade_delta = desired - current
            elif desired != current:
                if abs(early_ret) >= tau:
                    trade_delta = desired - current
                else:
                    trade_delta = 0
            else:
                trade_delta = 0

            if trade_delta != 0:
                total_cost += abs(trade_delta) * cost_per_contract
                pos = desired
                trade_fills.append((fill_price, pos))
                trades += 1
                if current != 0 and desired != current:
                    flips += 1

        if not trade_fills:
            return -total_cost, trades, flips

        gross = 0.0
        for i in range(len(trade_fills) - 1):
            entry_price, entry_pos = trade_fills[i]
            next_price = trade_fills[i + 1][0]
            gross += entry_pos * (next_price - entry_price)

        final_close = self._final_close
        last_price, last_pos = trade_fills[-1]
        gross += last_pos * (final_close - last_price)

        return gross - total_cost, trades, flips

    @staticmethod
    def _median_abs(values: list[float]) -> float:
        if not values:
            return 0.0
        ordered = sorted(abs(v) for v in values)
        n = len(ordered)
        if n % 2:
            return ordered[n // 2]
        return (ordered[n // 2 - 1] + ordered[n // 2]) / 2.0

    @staticmethod
    def _quantile_abs(rows: list[tuple[float, float]], q: float) -> float:
        values = sorted(abs(r[0]) for r in rows)
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
        if early_ret > 0:
            return self._sign
        return -self._sign

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
            self._first_open = bar.open
            self._evaluated_this_session = False

        self._bars_this_session += 1

        if not ctx.actionable:
            return
        if ctx.working_order_ids:
            return
        if self._evaluated_this_session:
            return
        if self._bars_this_session < self._lookback:
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
            # Only flip on a strong contrary signal. We already have the
            # desired direction and the fitted threshold from setup().
            if abs(early_ret) >= self._flip_tau:
                self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
