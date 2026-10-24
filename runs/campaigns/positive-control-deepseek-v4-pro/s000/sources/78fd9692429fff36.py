"""Widen the fitted early-session direction search to every minute interval.

Prior trial #61, the best so far, used a fixed 5m interval and scored +0.078.
The later searches only tried a sparse set of intervals (1m, 2m, 3m, 5m, 10m,
15m, 30m). This keeps the validated low-turnover, once-per-session,
hold-across-sessions shape and only makes the interval search dense: every
minute from 1m to 30m. If the true optimum is e.g. 4m, 6m or 7m, previous
searches never evaluated it.
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Fitted early-session direction with a dense interval search."""

    def __init__(
        self,
        *,
        size: int = 3,
        max_lookback: int = 12,
        flip_threshold_quantile: float = 0.25,
        min_samples: int = 12,
        interval_candidates: tuple[str, ...] | None = None,
    ) -> None:
        self.size = size
        self.max_lookback = max_lookback
        self.flip_threshold_quantile = flip_threshold_quantile
        self.min_samples = min_samples

        if interval_candidates is None:
            interval_candidates = tuple(f"{m}m" for m in range(1, 31))
        self.interval_candidates = interval_candidates

        # Fitted in setup()
        self.interval = "5m"
        self._lookback = 1
        self._sign = 1
        self._drift_direction = 1
        self._flip_tau = 0.0

        # Live session state
        self._session: int | None = None
        self._bars_this_session = 0
        self._session_bars: list[Any] = []
        self._evaluated_this_session = False

    # ------------------------------------------------------------------
    # Setup and fitting
    # ------------------------------------------------------------------
    def setup(self, ctx) -> None:
        best = None

        for interval in self.interval_candidates:
            try:
                bars = tuple(ctx.train_bars(interval))
            except Exception:
                continue
            if not bars:
                continue

            fitted = self._fit_interval(bars)
            if fitted is None:
                continue

            score, lookback, sign, drift, flip_tau = fitted
            if best is None or score > best[0]:
                best = (score, interval, lookback, sign, drift, flip_tau)

        if best is not None:
            (
                _,
                self.interval,
                self._lookback,
                self._sign,
                self._drift_direction,
                self._flip_tau,
            ) = best

        ctx.subscribe(self.interval)

    def _fit_interval(
        self,
        bars: tuple[Any, ...],
    ) -> tuple[float, int, int, int, float] | None:
        sessions: dict[int, list[Any]] = {}
        for bar in bars:
            sessions.setdefault(bar.session, []).append(bar)
        ordered = sorted(sessions)

        if len(ordered) < self.min_samples:
            return None

        drift_direction = 1 if bars[-1].close >= bars[0].close else -1

        best_score: float | None = None
        best_params: tuple[int, int] | None = None

        for k in range(1, self.max_lookback + 1):
            rows: list[tuple[int, float, float]] = []
            for sid in ordered:
                sb = sessions[sid]
                if len(sb) < k:
                    continue
                entry_close = sb[k - 1].close
                early_ret = entry_close - sb[0].open
                if abs(early_ret) < 1e-12:
                    continue
                rows.append((sid, early_ret, entry_close))

            if len(rows) < self.min_samples:
                continue

            for sign in (1, -1):
                pnl = self._path_pnl(rows, sessions, ordered[-1], sign)
                score = pnl / len(rows)

                if best_score is None or score > best_score:
                    best_score = score
                    best_params = (k, sign)

        if best_score is None or best_params is None:
            return None

        lookback, sign = best_params

        # Rebuild the selected-lookback returns under a new name to compute the
        # flip threshold from plain floats.  Keeping the previous annotated
        # `rows` variable here caused mypy to treat elements as tuples.
        flip_returns: list[float] = []
        for sid in ordered:
            sb = sessions[sid]
            if len(sb) >= lookback:
                entry_close = sb[lookback - 1].close
                early_ret = entry_close - sb[0].open
                if abs(early_ret) >= 1e-12:
                    flip_returns.append(early_ret)

        flip_tau = self._quantile(
            [abs(v) for v in flip_returns], self.flip_threshold_quantile
        )
        return (best_score, lookback, sign, drift_direction, flip_tau)

    def _path_pnl(
        self,
        rows: list[tuple[int, float, float]],
        sessions: dict[int, list[Any]],
        last_sid: int,
        sign: int,
    ) -> float:
        """Continuous holding P&L for a fixed sign convention, one contract."""
        pnl = 0.0
        prev_desired: int | None = None
        prev_entry: float | None = None

        for _sid, early_ret, entry_close in rows:
            desired = sign if early_ret > 0 else -sign

            if prev_desired is not None and prev_entry is not None:
                pnl += (entry_close - prev_entry) * prev_desired

            prev_desired = desired
            prev_entry = entry_close

        if prev_desired is not None and prev_entry is not None:
            last_close = sessions[last_sid][-1].close
            pnl += (last_close - prev_entry) * prev_desired

        return pnl

    def _quantile(self, values: list[float], q: float) -> float:
        if not values:
            return 0.0
        values = sorted(values)
        idx = int(q * len(values))
        if idx >= len(values):
            idx = len(values) - 1
        return values[idx]

    # ------------------------------------------------------------------
    # Live callbacks
    # ------------------------------------------------------------------
    def _apply_desired(self, ctx, desired: int) -> None:
        """Move from the current position to desired * size contracts."""
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

        # Exact zero is rare; treat it as no signal and establish full-sample
        # drift only when flat.
        if abs(early_ret) < 1e-12:
            if pos == 0:
                self._apply_desired(ctx, self._drift_direction)
            return

        desired = self._sign if early_ret > 0 else -self._sign
        current = 1 if pos > 0 else -1 if pos < 0 else 0

        if current == 0:
            self._apply_desired(ctx, desired)
        elif desired != current:
            # Flip only on a strong contrary signal; small signals are likely
            # noise and turnover is not free.
            if abs(early_ret) >= self._flip_tau:
                self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
