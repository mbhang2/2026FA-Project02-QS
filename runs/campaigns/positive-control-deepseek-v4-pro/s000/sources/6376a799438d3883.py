"""Fitted early-session direction with a two-feature linear signal.

The prior best strategy (#34) used only the early session return,
    ret = close(bar k) - open(bar 0),
to choose +1 or -1 and held the position nearly continuously.  This version
keeps that continuous-exposure shape but lets the signal be a small linear
combination:

    score = w_ret * ret + w_prev * previous_ret

The weights, lookback, bar interval, and a no-trade deadband tau are fitted
from the training split in `setup()` by simulating the same continuous-holding
fill path used at runtime (T+1 fills at the next bar's open).  The objective
is net P&L after a small per-trade penalty, so the optimizer prefers fewer
flips when two configurations are close.

Runtime safety:
- action only when `ctx.actionable` and no working orders exist
- no order ids are kept; the position is moved with market orders sized to the
  desired +1/-1 position
- if the signal is inside the deadband and we are flat, the full-sample drift
  direction is used so the strategy remains exposed rather than idle
"""

from __future__ import annotations

from math import inf
from typing import Any


class Strategy:
    """Fitted linear early-session signal, held with minimal turnover."""

    def __init__(
        self,
        *,
        intervals: tuple[str, ...] = ("5m", "2m", "1m"),
        max_lookback: int = 8,
        min_samples: int = 50,
        fill_penalty: float = 0.0015,
        tau_quantiles: tuple[float, ...] = (0.0, 0.25, 0.5, 0.75, 1.0),
        size: int = 1,
    ) -> None:
        self.intervals = intervals
        self.max_lookback = max_lookback
        self.min_samples = min_samples
        self.fill_penalty = fill_penalty
        self.tau_quantiles = tau_quantiles
        self.size = size

        # Fitted in setup()
        self._interval = "5m"
        self._lookback = 1
        self._w_ret = 1.0
        self._w_prev = 0.0
        self._tau = 0.0
        self._drift_direction = 1

        # Runtime state
        self._session: int | None = None
        self._bars_this_session = 0
        self._session_bars: list[Any] = []
        self._evaluated_this_session = False

        # Cross-session signal memory
        self._prev_ret = 0.0
        self._last_ret = 0.0

    # ------------------------------------------------------------------
    # Setup / fitting
    # ------------------------------------------------------------------
    def setup(self, ctx) -> None:
        config = self._fit(ctx)

        if config is not None:
            (
                self._interval,
                self._lookback,
                self._w_ret,
                self._w_prev,
                self._tau,
                self._drift_direction,
            ) = config

        ctx.subscribe(self._interval)

    def _fit(self, ctx):
        """Fit the interval and linear signal by simulating the runtime path.

        Returns a configuration tuple or None if no usable configuration was
        found (fallback to the original single-feature shape).
        """
        weight_ret_values = (-2.0, -1.0, -0.5, 0.5, 1.0, 2.0)
        weight_prev_values = (-1.0, -0.5, 0.0, 0.5, 1.0)

        best_net: float | None = None
        best_trades: int | None = None
        best_config = None

        for interval in self.intervals:
            try:
                bars = tuple(ctx.train_bars(interval))
            except Exception:
                continue

            if not bars:
                continue

            # Full-sample drift used when the linear signal is inside the
            # deadband and we are not already holding a position.
            drift_direction = 1 if bars[-1].close >= bars[0].close else -1
            final_close = bars[-1].close

            sessions: dict[int, list[Any]] = {}
            for bar in bars:
                sessions.setdefault(bar.session, []).append(bar)

            for lookback in range(1, self.max_lookback + 1):
                rows = self._build_rows(lookback, sessions)
                if len(rows) < self.min_samples:
                    continue

                abs_rets = [abs(row[1]) for row in rows]
                tau_values = {
                    self._quantile_abs(abs_rets, q) for q in self.tau_quantiles
                }

                for w_ret in weight_ret_values:
                    for w_prev in weight_prev_values:
                        for tau in tau_values:
                            pnl, trades = self._evaluate_rows(
                                rows,
                                final_close,
                                drift_direction,
                                w_ret,
                                w_prev,
                                tau,
                            )
                            net = pnl - self.fill_penalty * trades

                            if best_net is None or net > best_net or (
                                abs(net - best_net) < 1e-12
                                and (best_trades is None or trades < best_trades)
                            ):
                                best_net = net
                                best_trades = trades
                                best_config = (
                                    interval,
                                    lookback,
                                    w_ret,
                                    w_prev,
                                    tau,
                                    drift_direction,
                                )

        return best_config

    def _build_rows(
        self, lookback: int, sessions: dict[int, list[Any]]
    ) -> list[tuple[int, float, float]]:
        """Build (session_id, early_ret, next_open) rows.

        A row is usable only when the session has at least `lookback + 1` bars,
        so the market order submitted on bar `lookback` has a modelled next-bar
        open fill.
        """
        rows: list[tuple[int, float, float]] = []

        for sid in sorted(sessions.keys()):
            sb = sessions[sid]
            if len(sb) < lookback + 1:
                continue
            ret = sb[lookback - 1].close - sb[0].open
            next_open = sb[lookback].open
            rows.append((sid, ret, next_open))

        return rows

    def _quantile_abs(self, values: list[float], q: float) -> float:
        """Upper deadband from a quantile of absolute early returns."""
        if not values:
            return 0.0
        if q <= 0.0:
            return 0.0
        if q >= 1.0:
            return inf

        ordered = sorted(values)
        idx = int(q * len(ordered))
        if idx >= len(ordered):
            idx = len(ordered) - 1
        return ordered[idx]

    def _evaluate_rows(
        self,
        rows: list[tuple[int, float, float]],
        final_close: float,
        drift_direction: int,
        w_ret: float,
        w_prev: float,
        tau: float,
    ) -> tuple[float, int]:
        """Simulate continuous exposure with next-bar-open fills.

        The first row always establishes a position, mirroring the runtime
        fallback to the drift direction.  Later rows flip only when the linear
        score is outside the deadband.
        """
        pnl = 0.0
        trades = 0
        holding = False
        current = 0
        entry = 0.0
        prev_ret = 0.0

        for _sid, ret, next_open in rows:
            score = w_ret * ret + w_prev * prev_ret

            if not holding:
                if score > tau:
                    desired = 1
                elif score < -tau:
                    desired = -1
                else:
                    desired = drift_direction

                holding = True
                current = desired
                entry = next_open
                trades += 1
                prev_ret = ret
                continue

            if score > tau:
                desired = 1
            elif score < -tau:
                desired = -1
            else:
                desired = current

            if desired != current:
                pnl += (next_open - entry) * current
                current = desired
                entry = next_open
                trades += 1

            prev_ret = ret

        if holding:
            pnl += (final_close - entry) * current

        return pnl, trades

    # ------------------------------------------------------------------
    # Runtime helpers
    # ------------------------------------------------------------------
    def _apply_desired(self, ctx, desired: int) -> None:
        """Queue the market order needed to move to the desired +1 or -1."""
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
        elif desired < 0:
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
        if self._interval not in ctx.bars:
            return

        bar = ctx.bars[self._interval]

        # Roll session state and cross-session signal memory.
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_this_session = 0
            self._session_bars = []
            self._evaluated_this_session = False
            self._prev_ret = self._last_ret

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
        ret = bar.close - first_open
        self._last_ret = ret

        score = self._w_ret * ret + self._w_prev * self._prev_ret

        pos = ctx.position
        current = 1 if pos > 0 else -1 if pos < 0 else 0

        if score > self._tau:
            desired = 1
        elif score < -self._tau:
            desired = -1
        else:
            desired = current if current != 0 else self._drift_direction

        if desired != current:
            self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
