"""Next proposal: fitted early-session direction with a conviction threshold.

Keeps the proven #61 shape -- one decision per session at a fitted lookback,
position held across sessions, flip only on strong contrary signals -- but adds
a minimum early-return magnitude before the first entry. The first entry from
flat is skipped when the early return is small; that threshold is taken from a
training-data quantile (default 20th percentile of absolute early returns).
Small early returns are the most likely to be noise, so this should reduce
turnover while keeping the same fitted directional edge.
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Fitted early-session direction with entry conviction threshold."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 3,
        max_lookback: int = 8,
        entry_quantile: float = 0.20,
        flip_quantile: float = 0.25,
        min_samples: int = 15,
    ) -> None:
        self.interval = interval
        self.size = size
        self.max_lookback = max_lookback
        self.entry_quantile = entry_quantile
        self.flip_quantile = flip_quantile
        self.min_samples = min_samples

        # Fitted in setup()
        self._lookback = 1
        self._sign = 1
        self._drift_direction = 1
        self._entry_tau = 0.0
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
        """Fit lookback, sign, and thresholds from training 5m bars."""
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

        best_score: float | None = None
        best_k = 1
        best_sign = 1
        best_rows: list[tuple[int, float, float]] | None = None

        for k in range(1, self.max_lookback + 1):
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
                score = self._eval_full_switch(
                    rows, sessions, sorted_sessions[-1], sign
                )
                if best_score is None or score > best_score:
                    best_score = score
                    best_k = k
                    best_sign = sign
                    best_rows = rows

        if best_rows is not None:
            self._lookback = best_k
            self._sign = best_sign
            self._entry_tau = self._quantile_of_abs_returns(
                best_rows, self.entry_quantile
            )
            self._flip_tau = self._quantile_of_abs_returns(
                best_rows, self.flip_quantile
            )
        else:
            self._lookback = 1
            self._sign = 1
            self._entry_tau = 0.0
            self._flip_tau = 0.0

    def _eval_full_switch(self, rows, sessions, last_sid, sign) -> float:
        """Approximate continuous-holding P&L for a fixed sign convention."""
        pnl = 0.0
        prev_desired: int | None = None
        prev_entry: float | None = None

        for _sid, early_ret, entry_close in rows:
            desired = 1 if sign * early_ret > 0 else -1

            if prev_desired is not None and prev_entry is not None:
                pnl += (entry_close - prev_entry) * prev_desired

            prev_desired = desired
            prev_entry = entry_close

        if prev_desired is not None and prev_entry is not None:
            last_close = sessions[last_sid][-1].close
            pnl += (last_close - prev_entry) * prev_desired

        return pnl

    def _quantile_of_abs_returns(
        self, rows: list[tuple[int, float, float]], q: float
    ) -> float:
        values = sorted(abs(r[1]) for r in rows)
        if not values:
            return 0.0
        idx = int(q * (len(values) - 1))
        if idx < 0:
            idx = 0
        if idx >= len(values):
            idx = len(values) - 1
        return values[idx]

    def _direction_for_early_ret(self, early_ret: float) -> int:
        """Desired position sign for a non-zero early return."""
        if early_ret > 0:
            return self._sign
        return -self._sign

    def _apply_desired(self, ctx, desired: int) -> None:
        """Queue the market order needed to move to the desired position."""
        pos = ctx.position
        if desired == 0:
            return

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
        current = 1 if pos > 0 else -1 if pos < 0 else 0

        # Zero or unmeasurably small return: no new exposure, no flip.
        if abs(early_ret) < 1e-12:
            return

        desired = self._direction_for_early_ret(early_ret)

        if current == 0:
            # Enter from flat only on a return strong enough to clear the
            # training-data conviction threshold.
            if abs(early_ret) >= self._entry_tau:
                self._apply_desired(ctx, desired)
        elif desired != current:
            # Flip only on a strong contrary signal.
            if abs(early_ret) >= self._flip_tau:
                self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
