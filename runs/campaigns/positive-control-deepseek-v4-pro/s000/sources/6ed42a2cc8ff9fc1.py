"""Fitted early-session direction on finer bars, held with minimal turnover.

Prior best (#34) achieved a small positive score with a continuous +1/-1
position chosen at the first 5-minute bar of each session. On this simulation a
session is short enough that the 5m view gives exactly one decision point per
session. This strategy keeps the same low-turnover shape but subscribes to 30s
bars and searches the first k bars for the strongest early-session return
signal, rather than always using the first 5 minutes. Fitting remains
out-of-sample in the engine's setup/run split sense; the runtime then evaluates
once per session at the fitted bar, enters when flat, and flips only on
strong signals above a fitted threshold.
"""

from __future__ import annotations


class Strategy:
    """Fitted early-session direction, held with minimal turnover."""

    def __init__(
        self,
        *,
        interval: str = "30s",
        size: int = 1,
        max_lookback: int = 12,
        flip_threshold_quantile: float = 0.25,
        min_samples: int = 10,
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

        # Runtime session state
        self._session: int | None = None
        self._bars_this_session = 0
        self._session_bars: list = []
        self._evaluated_this_session = False

        # Bar interval length in seconds, used for end-of-session fallback.
        self._bar_seconds = self._parse_interval_seconds(interval)

    @staticmethod
    def _parse_interval_seconds(interval: str) -> float:
        """Best-effort parse of the usual bar interval spellings."""
        if not isinstance(interval, str):
            return 60.0
        if interval.endswith("ms"):
            return float(interval[:-2]) / 1000.0
        if interval.endswith("s"):
            return float(interval[:-1])
        if interval.endswith("m"):
            return float(interval[:-1]) * 60.0
        if interval.endswith("h"):
            return float(interval[:-1]) * 3600.0
        if interval.endswith("d"):
            return float(interval[:-1]) * 86400.0
        return 60.0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        self._fit(ctx)

    # ------------------------------------------------------------------
    # Setup / fitting
    # ------------------------------------------------------------------
    def _fit(self, ctx) -> None:
        """Fit lookback, sign, and flip threshold from bulk training bars."""
        try:
            bars = tuple(ctx.train_bars(self.interval))
        except Exception:
            bars = ()

        if not bars:
            return

        self._drift_direction = 1 if bars[-1].close >= bars[0].close else -1

        sessions: dict[int, list] = {}
        for bar in bars:
            sessions.setdefault(bar.session, []).append(bar)
        sorted_sessions = sorted(sessions.keys())

        best_key = None
        best_lookback = 1
        best_sign = 1
        best_rows = None

        for k in range(1, self.max_lookback + 1):
            rows: list[tuple[int, float, float]] = []
            for sid in sorted_sessions:
                sb = sessions[sid]
                if len(sb) < k:
                    continue
                early_ret = sb[k - 1].close - sb[0].open
                if abs(early_ret) < 1e-12:
                    continue
                rows.append((sid, early_ret, sb[k - 1].close))

            if len(rows) < self.min_samples:
                continue

            for sign in (1, -1):
                score, flips = self._eval_sign(
                    rows, sessions, sorted_sessions[-1], sign
                )
                # Prefer better score, then fewer flips, then smaller lookback.
                key = (score, -flips, -k)
                if best_key is None or key > best_key:
                    best_key = key
                    best_lookback = k
                    best_sign = sign
                    best_rows = rows

        if best_rows is not None:
            self._lookback = best_lookback
            self._sign = best_sign
            self._flip_tau = self._quantile_abs(
                best_rows, self.flip_threshold_quantile
            )

    def _eval_sign(self, rows, sessions, last_sid, sign):
        """Evaluate continuous holding P&L for a fixed sign convention.

        Rows are (session_id, early_ret, entry_close). Assumes entry fills at
        the signal bar's close and is held until the next signal bar's close,
        which mirrors the runtime's continuous exposure.
        """
        pnl = 0.0
        prev_desired = None
        prev_entry = None
        flips = 0

        for _sid, early_ret, entry_close in rows:
            desired = 1 if sign * early_ret > 0 else -1

            if prev_desired is not None and prev_entry is not None:
                pnl += (entry_close - prev_entry) * prev_desired
                if desired != prev_desired:
                    flips += 1

            prev_desired = desired
            prev_entry = entry_close

        if prev_desired is not None and prev_entry is not None:
            last_close = sessions[last_sid][-1].close
            pnl += (last_close - prev_entry) * prev_desired

        return pnl, flips

    @staticmethod
    def _quantile_abs(rows, q: float) -> float:
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
            self._session_bars = []
            self._evaluated_this_session = False

        self._session_bars.append(bar)
        self._bars_this_session += 1

        if not ctx.actionable:
            return
        if ctx.working_order_ids:
            return
        if self._evaluated_this_session:
            return

        # If we are near the session end, evaluate with the bars we have,
        # rather than waiting forever for a fitted lookback that may not fit
        # a short session. This keeps the strategy exposed instead of missing
        # a whole session.
        near_end = (
            ctx.session_end - ctx.ts
            < 2 * self._bar_seconds * 1_000_000_000
        )
        if (self._bars_this_session < self._lookback) and not near_end:
            return

        self._evaluated_this_session = True

        first_open = self._session_bars[0].open
        early_ret = bar.close - first_open
        pos = ctx.position

        # No signal: if flat, use the full-sample drift direction so exposure
        # is established rather than waiting indefinitely.
        if abs(early_ret) < 1e-12:
            if pos == 0:
                self._apply_desired(ctx, self._drift_direction)
            return

        desired = self._direction_for_early_ret(early_ret)
        current = 1 if pos > 0 else -1 if pos < 0 else 0

        if current == 0:
            self._apply_desired(ctx, desired)
        elif desired != current and abs(early_ret) >= self._flip_tau:
            self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
