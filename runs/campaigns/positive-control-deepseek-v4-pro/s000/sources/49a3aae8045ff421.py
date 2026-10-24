"""Interval-optimised early-session direction, holding with low turnover.

This refines the best prior shape in this campaign (#61). The core remains:
decide once per session from the direction of the early return, hold across
sessions, and flip only on a strong opposite signal. The change is that the
fitting step now searches over several bar intervals, as well as lookback and
sign, using the same continuous-holding P&L objective that made #61 positive.
The chosen interval is then subscribed for the live walk.

The intention is to let the data decide whether entering on 30s, 1m, 2m, 5m, or
longer bars is better, rather than fixing 5m a priori. The rest of the
behaviour is deliberately conservative: size 3, no entry threshold, flip
threshold at a low quantile of early returns, and no stop or target so the
position stays in the fitted multi-session drift.
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Fitted early-session direction with interval selection."""

    def __init__(
        self,
        *,
        interval_pool: tuple[str, ...] = (
            "5m",
            "2m",
            "1m",
            "30s",
            "10m",
            "15m",
            "30m",
        ),
        size: int = 3,
        max_lookback: int = 15,
        flip_threshold_quantile: float = 0.25,
        min_samples: int = 12,
    ) -> None:
        self.interval_pool = interval_pool
        self.size = size
        self.max_lookback = max_lookback
        self.flip_threshold_quantile = flip_threshold_quantile
        self.min_samples = min_samples

        # Fitted in setup()
        self.interval = "5m"
        self._lookback = 1
        self._sign = 1
        self._drift_direction = 1
        self._flip_tau = 0.0

        # Per-session runtime state
        self._session: int | None = None
        self._bars_this_session = 0
        self._first_open = 0.0
        self._evaluated_this_session = False

    def setup(self, ctx) -> None:
        self._fit(ctx)
        ctx.subscribe(self.interval)

    # ------------------------------------------------------------------
    # Fitting
    # ------------------------------------------------------------------
    def _fit(self, ctx) -> None:
        """Fit interval, lookback, sign, and flip threshold from training bars."""
        best: tuple[float, str, int, int, int, list[Any]] | None = None

        for interval in self.interval_pool:
            try:
                bars = tuple(ctx.train_bars(interval))
            except Exception:
                bars = ()

            if not bars:
                continue

            sessions: dict[int, list[Any]] = {}
            for bar in bars:
                sessions.setdefault(bar.session, []).append(bar)

            ordered = sorted(sessions)
            if len(ordered) < self.min_samples:
                continue

            max_k = min(self.max_lookback, min(len(sessions[s]) for s in ordered))
            if max_k < 1:
                continue

            final_close = float(bars[-1].close)
            drift_dir = 1 if final_close >= float(bars[0].close) else -1

            for k in range(1, max_k + 1):
                rows: list[tuple[int, float, float]] = []
                for sid in ordered:
                    sb = sessions[sid]
                    if len(sb) < k:
                        continue
                    first_open = float(sb[0].open)
                    close_k = float(sb[k - 1].close)
                    early_ret = close_k - first_open
                    # Zero-return sessions produce no desired side; they are
                    # excluded from fitting and handled at runtime by drift.
                    if abs(early_ret) < 1e-12:
                        continue
                    rows.append((sid, early_ret, close_k))

                if len(rows) < self.min_samples:
                    continue

                for sign in (1, -1):
                    score = self._eval_sign(rows, sign, final_close)
                    if best is None or score > best[0]:
                        best = (score, interval, k, sign, drift_dir, rows)

        if best is not None:
            _, self.interval, self._lookback, self._sign, self._drift_direction, rows = best
            self._flip_tau = self._quantile_abs(rows, self.flip_threshold_quantile)
        else:
            self.interval = "5m"
            self._lookback = 1
            self._sign = 1
            self._drift_direction = 1
            self._flip_tau = 0.0

    def _eval_sign(self, rows: list[tuple[int, float, float]], sign: int, final_close: float) -> float:
        """Continuous holding P&L for a fixed sign convention."""
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
            pnl += (final_close - prev_entry) * prev_desired

        return pnl

    def _quantile_abs(self, rows: list[tuple[int, float, float]], q: float) -> float:
        values = sorted(abs(r[1]) for r in rows)
        if not values:
            return 0.0
        idx = int(q * len(values))
        if idx >= len(values):
            idx = len(values) - 1
        return float(values[idx])

    # ------------------------------------------------------------------
    # Runtime
    # ------------------------------------------------------------------
    def _desired(self, early_ret: float) -> int:
        if early_ret > 1e-12:
            return self._sign
        if early_ret < -1e-12:
            return -self._sign
        return 0

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
            self._first_open = float(bar.open)
            self._evaluated_this_session = False

        self._bars_this_session += 1

        if not ctx.actionable:
            return
        if ctx.working_order_ids:
            return
        if self._evaluated_this_session or self._bars_this_session != self._lookback:
            return

        self._evaluated_this_session = True

        early_ret = float(bar.close) - self._first_open
        pos = ctx.position
        current = 1 if pos > 0 else -1 if pos < 0 else 0

        # Flat early return: if flat, establish exposure in the full-sample
        # drift direction; if already in position, do nothing.
        if abs(early_ret) < 1e-12:
            if current == 0:
                self._apply_desired(ctx, self._drift_direction)
            return

        desired = self._desired(early_ret)

        if current == 0:
            self._apply_desired(ctx, desired)
        elif desired != current:
            # Only flip on a strong contrary signal.
            if abs(early_ret) >= self._flip_tau:
                self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
