"""Search over early-session bar signals and choose the best on the training split.

This builds on the best prior trials (#60/#61/#76), which fitted a per-session
direction from the first k bars and then held with minimal turnover. The change
here is that the fit is allowed to choose:

- the bar interval (30s, 1m or 5m),
- the lookback k,
- the sign convention,
- a signed-volume adjustment weight,
- the flip threshold quantile.

The signal used is raw = early_return + weight * signed_delta_norm, where
signed_delta_norm is the early-session order-flow delta divided by volume.
Candidates are evaluated on the train split with an approximate continuous
holding P&L, and a small per-trade penalty keeps the chosen strategy in the
low-turnover region that has scored best in this campaign.
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Fitted early-session direction with order-flow confirmation."""

    def __init__(
        self,
        *,
        size: int = 3,
        max_lookback: int = 12,
        min_samples: int = 15,
    ) -> None:
        self.size = size
        self.max_lookback = max_lookback
        self.min_samples = min_samples

        # Fitted in setup()
        self.interval = "5m"
        self._lookback = 1
        self._sign = 1
        self._drift_direction = 1
        self._delta_weight = 0.0
        self._flip_tau = 0.0

        # Search space for the fit
        self._interval_candidates = ("30s", "1m", "5m")
        self._delta_weights = (-2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0)
        self._flip_quantiles = (0.15, 0.20, 0.25, 0.30, 0.40, 0.50)
        self._trade_penalty = 0.0002

        # Per-session state
        self._session: int | None = None
        self._bars_this_session = 0
        self._session_bars: list[Any] = []
        self._evaluated_this_session = False

    def setup(self, ctx) -> None:
        self._fit(ctx)
        ctx.subscribe(self.interval)

    def _fit(self, ctx) -> None:
        """Fit interval, lookback, sign, delta weight, and flip threshold."""
        best: tuple[float, str, int, int, float, int, float, float, int] | None = None

        for interval in self._interval_candidates:
            try:
                bars = tuple(ctx.train_bars(interval))
            except Exception:
                continue
            if not bars:
                continue

            sessions: dict[int, list[Any]] = {}
            for bar in bars:
                sessions.setdefault(bar.session, []).append(bar)
            sorted_sessions = sorted(sessions)

            if len(sorted_sessions) < self.min_samples:
                continue

            drift_direction = 1 if bars[-1].close >= bars[0].close else -1
            max_bars = max(len(sessions[s]) for s in sorted_sessions)

            for k in range(1, min(self.max_lookback, max_bars) + 1):
                rows: list[tuple[int, float, float, float]] = []
                for sid in sorted_sessions:
                    sb = sessions[sid]
                    if len(sb) < k + 1:
                        continue
                    first_open = sb[0].open
                    close = sb[k - 1].close
                    early_ret = close - first_open

                    delta = sum(b.delta for b in sb[:k])
                    volume = sum(b.volume for b in sb[:k])
                    norm_delta = delta / volume if volume > 0 else 0.0

                    rows.append((sid, early_ret, close, norm_delta))

                if len(rows) < self.min_samples:
                    continue

                for dw in self._delta_weights:
                    raw_rows: list[tuple[int, float, float, float, float]] = []
                    for sid, er, close, nd in rows:
                        raw = er + dw * nd
                        raw_rows.append((sid, raw, close, nd, er))

                    for sign in (1, -1):
                        for fq in self._flip_quantiles:
                            flip_tau = self._quantile_abs(raw_rows, fq)
                            pnl, trades = self._eval_rows(
                                raw_rows,
                                sessions,
                                sorted_sessions[-1],
                                sign,
                                drift_direction,
                                flip_tau,
                            )
                            proxy = pnl - self._trade_penalty * trades

                            if best is None or proxy > best[0]:
                                best = (
                                    proxy,
                                    interval,
                                    k,
                                    sign,
                                    dw,
                                    drift_direction,
                                    flip_tau,
                                    pnl,
                                    trades,
                                )

        if best is not None:
            (
                _,
                self.interval,
                self._lookback,
                self._sign,
                self._delta_weight,
                self._drift_direction,
                self._flip_tau,
                _pnl,
                _trades,
            ) = best
        else:
            self.interval = "5m"
            self._lookback = 1
            self._sign = 1
            self._drift_direction = 1
            self._delta_weight = 0.0
            self._flip_tau = 0.0

    def _quantile_abs(
        self, raw_rows: list[tuple[int, float, float, float, float]], q: float
    ) -> float:
        """Quantile of non-zero absolute raw signals."""
        values = sorted(abs(r[1]) for r in raw_rows if abs(r[1]) > 1e-12)
        if not values:
            return 0.0

        if q <= 0.0:
            return values[0]
        if q >= 1.0:
            return values[-1]

        idx = int(q * (len(values) - 1))
        if idx < 0:
            idx = 0
        if idx >= len(values):
            idx = len(values) - 1
        return values[idx]

    def _eval_rows(
        self,
        raw_rows: list[tuple[int, float, float, float, float]],
        sessions: dict[int, list[Any]],
        last_sid: int,
        sign: int,
        drift_direction: int,
        flip_tau: float,
    ) -> tuple[float, int]:
        """Approximate continuous-holding P&L for a fixed rule."""
        pnl = 0.0
        pos = 0
        trades = 0
        prev_entry: float | None = None

        for _sid, raw, entry_close, _nd, _er in raw_rows:
            if prev_entry is not None:
                pnl += (entry_close - prev_entry) * pos

            prev_entry = entry_close

            if abs(raw) < 1e-12:
                if pos == 0:
                    pos = drift_direction
                    trades += 1
                # If already positioned, hold.
                continue

            desired = sign * (1 if raw > 0 else -1)

            if pos == 0:
                pos = desired
                trades += 1
            elif pos != desired and abs(raw) >= flip_tau:
                pos = desired
                trades += 1

        if prev_entry is not None:
            last_close = sessions[last_sid][-1].close
            pnl += (last_close - prev_entry) * pos

        return pnl, trades

    def _apply_desired(self, ctx, desired: int) -> None:
        """Queue the market order needed to reach the desired signed position."""
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

        used_bars = self._session_bars[: self._lookback]
        delta = sum(b.delta for b in used_bars)
        volume = sum(b.volume for b in used_bars)
        norm_delta = delta / volume if volume > 0 else 0.0

        raw = early_ret + self._delta_weight * norm_delta
        pos = ctx.position
        current = 1 if pos > 0 else -1 if pos < 0 else 0

        if abs(raw) < 1e-12:
            if pos == 0:
                self._apply_desired(ctx, self._drift_direction)
            return

        desired = self._sign * (1 if raw > 0 else -1)

        if current == 0:
            self._apply_desired(ctx, desired)
        elif desired != current and abs(raw) >= self._flip_tau:
            self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
