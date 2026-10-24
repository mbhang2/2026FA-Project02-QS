"""Scale the proven #61 fitted early-session direction strategy to size 4.

Prior trials #60 (size=2, score +0.0746) and #61 (size=3, score +0.0780)
show the same low-turnover, one-decision-per-session edge improved when size
increased. This proposal keeps the exact mechanics of #61 -- fit lookback and
sign from training 5m bars, evaluate once per session, hold across sessions
with no stop/target, and flip only on strong contrary signals -- while scaling
position size to 4.
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Fitted early-session direction, scaled to four contracts."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 4,
        max_lookback: int = 8,
        flip_threshold_quantile: float = 0.25,
        min_samples: int = 15,
    ) -> None:
        self.interval = interval
        self.size = size
        self.max_lookback = max_lookback
        self.flip_threshold_quantile = flip_threshold_quantile
        self.min_samples = min_samples

        # Fitted in setup()
        self._lookback = 1
        self._sign = 1
        self._drift_direction = 1
        self._flip_tau = 0.0

        # Session state
        self._session: int | None = None
        self._bars_this_session = 0
        self._session_bars: list[Any] = []
        self._evaluated_this_session = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        self._fit(ctx)

    def _fit(self, ctx) -> None:
        """Fit lookback, direction, and flip threshold from training bars."""
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
        sorted_sessions = sorted(sessions.keys())

        best_score = None
        best_k = 1
        best_sign = 1
        best_rows = None

        for k in range(1, self.max_lookback + 1):
            rows = []
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
                score = self._eval_sign(rows, sessions, sorted_sessions[-1], sign)
                if best_score is None or score > best_score:
                    best_score = score
                    best_k = k
                    best_sign = sign
                    best_rows = rows

        if best_rows is not None:
            self._lookback = best_k
            self._sign = best_sign
            self._flip_tau = self._quantile_of_abs_returns(
                best_rows, self.flip_threshold_quantile
            )
        else:
            self._lookback = 1
            self._sign = 1
            self._flip_tau = 0.0

    def _eval_sign(self, rows, sessions, last_sid, sign) -> float:
        """Evaluate continuous holding P&L for a fixed sign convention."""
        pnl = 0.0
        prev_desired = None
        prev_entry = None

        for sid, early_ret, entry_close in rows:
            desired = 1 if sign * early_ret > 0 else -1

            if prev_desired is not None and prev_entry is not None:
                pnl += (entry_close - prev_entry) * prev_desired

            prev_desired = desired
            prev_entry = entry_close

        if prev_desired is not None and prev_entry is not None:
            last_close = sessions[last_sid][-1].close
            pnl += (last_close - prev_entry) * prev_desired

        return pnl

    def _quantile_of_abs_returns(self, rows, q: float) -> float:
        values = sorted(abs(r[1]) for r in rows)
        if not values:
            return 0.0
        idx = int(q * (len(values) - 1))
        if idx >= len(values):
            idx = len(values) - 1
        return values[idx]

    def _direction_for_early_ret(self, early_ret: float) -> int:
        """Return desired position +1 or -1 for a non-zero early return."""
        if early_ret > 0:
            return self._sign
        return -self._sign

    def _apply_desired(self, ctx, desired: int) -> None:
        """Queue the market order needed to move from current position to desired."""
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
            if pos == 0:
                self._apply_desired(ctx, self._drift_direction)
            return

        desired = self._direction_for_early_ret(early_ret)
        current = 1 if pos > 0 else -1 if pos < 0 else 0

        if current == 0:
            self._apply_desired(ctx, desired)
        elif desired != current:
            if abs(early_ret) >= self._flip_tau:
                self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
