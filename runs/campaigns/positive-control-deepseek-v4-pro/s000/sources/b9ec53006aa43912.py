"""Next candidate in the fitted early-session drift family.

The best prior trials (#60/#61) fit an early-session drift on 5-minute bars and
made one decision per session, holding positions across sessions with flips only
on strong contrary early returns.  That low-turnover, continuous-holding shape
was the only family with a positive score in this campaign.

This version keeps the exact same runtime mechanics as #61 but:
  * increases position size from 3 to 4
  * broadens the lookback search slightly from 8 to 10
  * uses a slightly higher flip threshold quantile (0.30 vs 0.25)

The hope is that the per-contract edge persists and a slightly larger size
captures more of it without adding a new error surface.  Turnover is still
minimal: one evaluation per session, market orders only, positions held across
sessions.
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Fitted early-session drift, size=4, with slightly broader lookback."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 4,
        max_lookback: int = 10,
        flip_threshold_quantile: float = 0.30,
        min_samples: int = 20,
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

        # Per-session runtime state
        self._session: int | None = None
        self._bars_this_session = 0
        self._session_bars: list[Any] = []
        self._evaluated_this_session = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        self._fit(ctx)

    # ------------------------------------------------------------------
    # Fitting
    # ------------------------------------------------------------------
    def _fit(self, ctx) -> None:
        """Fit lookback, sign, drift direction, and flip threshold."""
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
                score = self._eval_sign(
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
            self._flip_tau = self._quantile_abs_returns(
                best_rows, self.flip_threshold_quantile
            )
        else:
            self._lookback = 1
            self._sign = 1
            self._flip_tau = 0.0

    def _eval_sign(self, rows, sessions, last_sid, sign) -> float:
        """Continuous-holding mark-to-market P&L for a fixed sign convention."""
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

    def _quantile_abs_returns(self, rows, q: float) -> float:
        values = sorted(abs(r[1]) for r in rows)
        if not values:
            return 0.0
        idx = int(q * len(values))
        if idx >= len(values):
            idx = len(values) - 1
        return values[idx]

    def _direction_for_early_ret(self, early_ret: float) -> int:
        if early_ret > 0:
            return self._sign
        return -self._sign

    def _apply_desired(self, ctx, desired: int) -> None:
        pos = int(ctx.position)
        size = int(self.size)

        if desired == 0:
            return

        if desired > 0:
            if pos == 0:
                ctx.buy(size=size, tif="DAY", tag="session-long")
            elif pos < 0:
                ctx.buy(size=-pos + size, tif="DAY", tag="session-flip-long")
        else:
            if pos == 0:
                ctx.sell(size=size, tif="DAY", tag="session-short")
            elif pos > 0:
                ctx.sell(size=pos + size, tif="DAY", tag="session-flip-short")

    # ------------------------------------------------------------------
    # Runtime
    # ------------------------------------------------------------------
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
        if len(self._session_bars) < self._lookback:
            return

        self._evaluated_this_session = True

        first_open = float(self._session_bars[0].open)
        early_ret = float(bar.close) - first_open
        pos = int(ctx.position)
        current = 1 if pos > 0 else -1 if pos < 0 else 0

        if abs(early_ret) < 1e-12:
            # Flat early return: establish full-sample drift when flat,
            # otherwise leave the existing position alone.
            if current == 0:
                self._apply_desired(ctx, self._drift_direction)
            return

        desired = self._direction_for_early_ret(early_ret)

        if current == 0:
            self._apply_desired(ctx, desired)
        elif desired != current:
            # Only flip on a strong contrary signal; small signals are likely
            # noise and turnover is not free.
            if abs(early_ret) >= self._flip_tau:
                self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
