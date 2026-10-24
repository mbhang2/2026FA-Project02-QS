"""Fitted early-session direction with entry/flip thresholds.

The best prior trial (#61) used the sign of the early session return
(first k bars of 5m data), entering on every non-zero signal and flipping only
when a contrary signal exceeded the 25th percentile of absolute early returns.
This kept the proven low-turnover shape: one decision per session, size 3,
held across sessions with no stop/target.

This proposal keeps that shape but fits three pieces more carefully in setup():

1. It selects the lookback k and the sign convention using the same training
   bars #61 used.
2. It intersects an *entry threshold* for fresh exposure: near-zero early
   returns are likely noise and do not currently inform the direction edge.
3. It fits the *flip threshold* together with the entry threshold using a
   Sharpe-like per-session objective, rather than fixing it at a single
   quantile.

All fitting happens in setup() via `ctx.train_bars`, which is the documented
place for parameter fitting on the training split. The live session then
evaluates once at bar k and otherwise behaves like the proven #61 strategy.
"""

from __future__ import annotations

from math import sqrt
from typing import Any


class Strategy:
    """Fitted early-session direction, held with minimal turnover."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 3,
        max_lookback: int = 8,
        entry_quantiles: tuple[float, ...] = (0.0, 0.1, 0.2, 0.3, 0.4),
        flip_quantiles: tuple[float, ...] = (
            0.1,
            0.2,
            0.3,
            0.4,
            0.5,
            0.6,
        ),
        min_samples: int = 12,
        min_trades: int = 5,
        max_trades: int = 250,
        exposure_floor: float = 0.65,
    ) -> None:
        self.interval = interval
        self.size = size
        self.max_lookback = max_lookback
        self.entry_quantiles = entry_quantiles
        self.flip_quantiles = flip_quantiles
        self.min_samples = min_samples
        self.min_trades = min_trades
        self.max_trades = max_trades
        self.exposure_floor = exposure_floor

        # Fitted in setup() from the training split.
        self._lookback = 1
        self._sign = 1
        self._drift_direction = 1
        self._entry_tau = 0.0
        self._flip_tau = 0.0

        # Live session state.
        self._session: int | None = None
        self._bars_this_session = 0
        self._session_bars: list[Any] = []
        self._evaluated_this_session = False

    # ------------------------------------------------------------------
    # Setup and fitting
    # ------------------------------------------------------------------
    def setup(self, ctx) -> None:
        # Choose the subscription interval before the live walk starts.
        # The default is the same 5m interval as the best prior trial.
        self._fit(ctx)
        ctx.subscribe(self.interval)

    def _fit(self, ctx) -> None:
        """Fit lookback, sign, entry threshold, and flip threshold."""
        try:
            bars = tuple(ctx.train_bars(self.interval))
        except Exception:
            bars = ()

        if bars:
            self._drift_direction = 1 if bars[-1].close >= bars[0].close else -1
        else:
            self._drift_direction = 1

        sessions: dict[int, list[Any]] = {}
        for bar in bars:
            sessions.setdefault(bar.session, []).append(bar)
        ordered = sorted(sessions)

        if len(ordered) < self.min_samples:
            self._lookback = 1
            self._sign = 1
            self._entry_tau = 0.0
            self._flip_tau = 0.0
            return

        best_score: float | None = None
        best_std = float("inf")
        best_trades = 10**9
        best_params: tuple[int, int, float, float] | None = None

        for k in range(1, self.max_lookback + 1):
            rows: dict[int, tuple[float, float, float]] = {}
            abs_vals: list[float] = []

            for sid in ordered:
                sb = sessions[sid]
                # Need the signal bar and one further bar for the realistic
                # execution price at the next bar's open.
                if len(sb) < k + 1:
                    continue

                first_open = sb[0].open
                early_ret = sb[k - 1].close - first_open
                fill_price = sb[k].open
                end_close = sb[-1].close

                rows[sid] = (early_ret, fill_price, end_close)
                if abs(early_ret) >= 1e-12:
                    abs_vals.append(abs(early_ret))

            if len(rows) < self.min_samples:
                continue

            abs_vals.sort()
            if not abs_vals:
                continue

            for sign in (1, -1):
                for entry_q in self.entry_quantiles:
                    entry_tau = self._quantile(abs_vals, entry_q)
                    for flip_q in self.flip_quantiles:
                        flip_tau = self._quantile(abs_vals, flip_q)

                        daily_pnl, _total_pnl, trades, exposed = self._simulate_path(
                            sessions,
                            ordered,
                            rows,
                            sign,
                            entry_tau,
                            flip_tau,
                        )

                        if trades < self.min_trades or trades > self.max_trades:
                            continue
                        if exposed < self.exposure_floor * len(ordered):
                            continue
                        if len(daily_pnl) < 3:
                            continue

                        mean = sum(daily_pnl) / len(daily_pnl)
                        variance = (
                            sum((x - mean) ** 2 for x in daily_pnl)
                            / len(daily_pnl)
                        )
                        std = sqrt(max(variance, 0.0))
                        score = mean / (std + 1e-12)

                        if (
                            best_score is None
                            or score > best_score + 1e-12
                            or (
                                abs(score - best_score) <= 1e-12
                                and (
                                    std < best_std - 1e-12
                                    or (
                                        abs(std - best_std) <= 1e-12
                                        and trades < best_trades
                                    )
                                )
                            )
                        ):
                            best_score = score
                            best_std = std
                            best_trades = trades
                            best_params = (k, sign, entry_tau, flip_tau)

        if best_params is not None:
            (
                self._lookback,
                self._sign,
                self._entry_tau,
                self._flip_tau,
            ) = best_params
        else:
            # Fallback: the proven #61 configuration.
            self._lookback = 1
            self._sign = 1
            self._entry_tau = 0.0
            self._flip_tau = 0.0

    def _quantile(self, values: list[float], q: float) -> float:
        if not values:
            return 0.0
        idx = int(q * (len(values) - 1))
        if idx < 0:
            idx = 0
        if idx >= len(values):
            idx = len(values) - 1
        return values[idx]

    def _simulate_path(
        self,
        sessions: dict[int, list[Any]],
        ordered: list[int],
        rows: dict[int, tuple[float, float, float]],
        sign: int,
        entry_tau: float,
        flip_tau: float,
    ) -> tuple[list[float], float, int, int]:
        """Simulate the strategy over the training sessions."""
        pos = 0
        cash = 0.0
        prev_close: float | None = None
        daily_pnl: list[float] = []
        trades = 0
        exposed = 0

        for sid in ordered:
            sb = sessions.get(sid)
            if not sb:
                daily_pnl.append(0.0)
                continue

            end_close = sb[-1].close

            start_mark = cash
            if pos != 0 and prev_close is not None:
                start_mark += pos * prev_close
            start_pos = pos

            row = rows.get(sid)
            if row is not None:
                early_ret, fill_price, _ = row

                if abs(early_ret) < 1e-12:
                    if pos == 0 and entry_tau <= 0:
                        desired = self._drift_direction
                    else:
                        desired = 1 if pos > 0 else -1 if pos < 0 else 0
                else:
                    desired = 1 if sign * early_ret > 0 else -1

                current = 0 if pos == 0 else (1 if pos > 0 else -1)
                signal_strength = abs(early_ret)

                if current == 0:
                    if signal_strength >= entry_tau:
                        new_pos = desired * self.size
                        delta = new_pos - pos
                        if delta != 0:
                            cash -= delta * fill_price
                            pos = new_pos
                            trades += 1
                elif desired != current:
                    if signal_strength >= flip_tau:
                        new_pos = desired * self.size
                        delta = new_pos - pos
                        if delta != 0:
                            cash -= delta * fill_price
                            pos = new_pos
                            trades += 1

            end_mark = cash
            if pos != 0:
                end_mark += pos * end_close

            daily_pnl.append(end_mark - start_mark)
            if start_pos != 0 or pos != 0:
                exposed += 1

            prev_close = end_close

        final_mark = cash
        if pos != 0 and prev_close is not None:
            final_mark += pos * prev_close

        return daily_pnl, final_mark, trades, exposed

    # ------------------------------------------------------------------
    # Live callbacks
    # ------------------------------------------------------------------
    def _apply_desired(self, ctx, desired: int) -> None:
        """Move from current position to desired * size contracts."""
        pos = ctx.position
        delta = desired * self.size - pos
        if delta == 0:
            return

        if delta > 0:
            ctx.buy(size=delta, tif="DAY", tag="session-direction")
        else:
            ctx.sell(size=-delta, tif="DAY", tag="session-direction")

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
        if self._evaluated_this_session or self._bars_this_session < self._lookback:
            return

        self._evaluated_this_session = True

        first_open = self._session_bars[0].open
        early_ret = bar.close - first_open
        pos = ctx.position

        if abs(early_ret) < 1e-12:
            if pos == 0 and self._entry_tau <= 0:
                self._apply_desired(ctx, self._drift_direction)
            return

        desired = 1 if self._sign * early_ret > 0 else -1
        current = 1 if pos > 0 else -1 if pos < 0 else 0

        if current == 0:
            if abs(early_ret) >= self._entry_tau:
                self._apply_desired(ctx, desired)
        elif desired != current:
            if abs(early_ret) >= self._flip_tau:
                self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
