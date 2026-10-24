"""Fitted early-session direction, interval-optimized.

Refines the best prior strategy in this campaign (#61). The core shape remains
intentionally low-turnover: decide once per session using the return from the
first lookback bars, hold across sessions, and flip only on a strong contrary
signal. The refinement is to let setup() choose the bar interval and lookback
that maximised in-sample holding P&L on the training split, rather than fixing
5-minute bars. Execution timing is approximated more faithfully by evaluating
the training sequence at the next bar's open, which is where a market order
submitted on the close of the decision bar fills.

The runtime follows the proven safe pattern: keep all per-session state on
self, act only when the chosen interval closes, and use ctx.actionable before
every order.
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Fitted session-direction strategy with interval selection."""

    def __init__(
        self,
        *,
        size: int = 3,
        max_lookback: int = 8,
        min_samples: int = 15,
        flip_quantile: float = 0.25,
        interval_candidates: tuple[str, ...] = ("5m", "1m", "30s"),
    ) -> None:
        self.size = size
        self.max_lookback = max_lookback
        self.min_samples = min_samples
        self.flip_quantile = flip_quantile
        self.interval_candidates = interval_candidates

        # Fitted in setup()
        self.interval = "5m"
        self._lookback = 1
        self._sign = 1
        self._drift_direction = 1
        self._flip_tau = 0.0

        # Per-session runtime state
        self._session: int | None = None
        self._bars_this_session = 0
        self._session_bars: list[Any] = []
        self._evaluated_this_session = False

    def setup(self, ctx) -> None:
        self._fit(ctx)
        ctx.subscribe(self.interval)

    def _fit(self, ctx) -> None:
        """Fit interval, lookback, sign, and flip threshold from training bars."""
        best: tuple[float, str, int, int, list[tuple[float, float]]] | None = None

        for interval in self.interval_candidates:
            try:
                bars = tuple(ctx.train_bars(interval))
            except Exception:
                continue
            if not bars:
                continue

            sessions: dict[int, list[Any]] = {}
            for bar in bars:
                sessions.setdefault(bar.session, []).append(bar)

            ordered = sorted(sessions)
            if not ordered:
                continue

            min_bars = min(len(sessions[s]) for s in ordered)
            # Need a bar after the decision bar for the next-open entry estimate.
            max_k = min(self.max_lookback, min_bars - 1)
            if max_k < 1:
                continue

            final_close = float(sessions[ordered[-1]][-1].close)

            for k in range(1, max_k + 1):
                rows: list[tuple[float, float]] = []
                for sid in ordered:
                    sb = sessions[sid]
                    if len(sb) <= k:
                        continue
                    first_open = float(sb[0].open)
                    early_ret = float(sb[k - 1].close) - first_open
                    entry_open = float(sb[k].open)
                    if abs(early_ret) < 1e-12:
                        continue
                    rows.append((early_ret, entry_open))

                if len(rows) < self.min_samples:
                    continue

                for sign in (1, -1):
                    pnl = self._eval_sign(rows, sign, final_close)
                    if best is None or pnl > best[0]:
                        best = (pnl, interval, k, sign, rows)

        if best is not None:
            _, self.interval, self._lookback, self._sign, best_rows = best
            self._flip_tau = self._quantile_abs(best_rows, self.flip_quantile)
        else:
            self.interval = "5m"
            self._lookback = 1
            self._sign = 1
            self._flip_tau = 0.0

        # Full-sample drift direction, used only on a perfectly flat early return.
        try:
            full = tuple(ctx.train_bars(self.interval))
            if full:
                self._drift_direction = 1 if full[-1].close >= full[0].close else -1
            else:
                self._drift_direction = 1
        except Exception:
            self._drift_direction = 1

    def _eval_sign(
        self,
        rows: list[tuple[float, float]],
        sign: int,
        final_close: float,
    ) -> float:
        """Continuous holding P&L for a fixed sign convention and entry timing."""
        pnl = 0.0
        prev_desired: int | None = None
        prev_entry: float | None = None

        for early_ret, entry_open in rows:
            desired = 1 if sign * early_ret > 0 else -1

            if prev_desired is not None and prev_entry is not None:
                pnl += (entry_open - prev_entry) * prev_desired

            prev_desired = desired
            prev_entry = entry_open

        if prev_desired is not None and prev_entry is not None:
            pnl += (final_close - prev_entry) * prev_desired

        return pnl

    def _quantile_abs(self, rows: list[tuple[float, float]], q: float) -> float:
        values = sorted(abs(r[0]) for r in rows)
        if not values:
            return 0.0
        idx = int(q * len(values))
        if idx >= len(values):
            idx = len(values) - 1
        return float(values[idx])

    def _direction_for_early_ret(self, early_ret: float) -> int:
        """Desired position +1/-1 for a non-zero early return."""
        if early_ret > 0:
            return self._sign
        return -self._sign

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

        # Perfectly flat early return: if already positioned, do nothing; if
        # flat, take the full-sample drift direction.
        if abs(early_ret) < 1e-12:
            if pos == 0:
                self._apply_desired(ctx, self._drift_direction)
            return

        desired = self._direction_for_early_ret(early_ret)
        current = 1 if pos > 0 else -1 if pos < 0 else 0

        if current == 0:
            self._apply_desired(ctx, desired)
        elif desired != current:
            # Only flip on a strong contrary signal; small signals are likely
            # noise and commission is not free.
            if abs(early_ret) >= self._flip_tau:
                self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
