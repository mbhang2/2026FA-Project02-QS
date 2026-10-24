"""A fitted linear session-level direction rule with previous-session context.

This builds on the best prior shape (trials #33, #34, #35): one position held
nearly continuously, with direction chosen early in each session and a fitted
flip threshold that suppresses marginal whipsaw.

Instead of a single sign on the early session return, setup fits a small linear
score

    z = w1 * (early_ret / scale) + w2 * (prev_early_ret / scale) + bias

and a flip threshold chosen from quantiles of |z|. The in-sample holding backtest
used for fitting models entry on the next bar's open (the engine's T+1 fill
rule), and subtracts a per-fill turnover penalty so flips must pay for
themselves. The result is a modest generalisation of #34/#35: the early return
can be scaled, reversed, blended with the previous signal session's early return,
and offset by a bias.
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Fitted early-session direction with previous-session context."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 1,
        max_lookback: int = 8,
        min_samples: int = 15,
        min_trades: int = 5,
        max_trades: int = 120,
        fill_penalty: float = 0.0004,
        weight_candidates: tuple[float, ...] = (
            -2.0,
            -1.0,
            -0.5,
            0.0,
            0.5,
            1.0,
            2.0,
        ),
        bias_candidates: tuple[float, ...] = (-1.0, -0.5, 0.0, 0.5, 1.0),
        quantile_candidates: tuple[float, ...] = (0.0, 0.25, 0.5),
    ) -> None:
        self.interval = interval
        self.size = size
        self.max_lookback = max_lookback
        self.min_samples = min_samples
        self.min_trades = min_trades
        self.max_trades = max_trades
        self.fill_penalty = fill_penalty
        self.weight_candidates = weight_candidates
        self.bias_candidates = bias_candidates
        self.quantile_candidates = quantile_candidates

        # Fitted in setup()
        self._lookback = 1
        self._w1 = 1.0
        self._w2 = 0.0
        self._bias = 0.0
        self._scale = 1.0
        self._flip_tau = 0.0
        self._drift_direction = 1

        # Session state
        self._session: int | None = None
        self._bars_this_session = 0
        self._session_bars: list[Any] = []
        self._evaluated_this_session = False
        self._prev_early = 0.0

    # ------------------------------------------------------------------
    # Setup / fitting
    # ------------------------------------------------------------------
    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        self._fit(ctx)

    def _fit(self, ctx) -> None:
        """Fit lookback, linear score weights, and flip threshold."""
        try:
            bars = tuple(ctx.train_bars(self.interval))
        except Exception:
            bars = ()

        if bars:
            self._drift_direction = 1 if bars[-1].close >= bars[0].close else -1
        else:
            self._drift_direction = 1

        if not bars:
            self._fallback()
            return

        sessions: dict[int, list[Any]] = {}
        for bar in bars:
            sessions.setdefault(bar.session, []).append(bar)

        sorted_sessions = sorted(sessions.keys())
        if not sorted_sessions:
            self._fallback()
            return

        last_close = sessions[sorted_sessions[-1]][-1].close

        best_net: float | None = None
        best_config: tuple[int, float, float, float, float, float] | None = None

        for k in range(1, self.max_lookback + 1):
            rows = self._build_rows(k, sessions)
            if len(rows) < self.min_samples:
                continue

            early_values = [r[0] for r in rows]
            scale = self._median_abs(early_values)
            if scale <= 1e-12:
                scale = 1.0

            for w1 in self.weight_candidates:
                for w2 in self.weight_candidates:
                    for bias in self.bias_candidates:
                        zs = [
                            w1 * (r[0] / scale) + w2 * (r[1] / scale) + bias
                            for r in rows
                        ]

                        for q in self.quantile_candidates:
                            tau = self._quantile_abs(zs, q)
                            gross, trades = self._evaluate_rows(
                                zs, rows, last_close, tau
                            )

                            if trades < self.min_trades:
                                continue
                            if trades > self.max_trades:
                                continue

                            net = gross - self.fill_penalty * trades
                            if best_net is None or net > best_net:
                                best_net = net
                                best_config = (k, w1, w2, bias, scale, tau)

        if best_config is None:
            self._fallback()
            return

        (
            self._lookback,
            self._w1,
            self._w2,
            self._bias,
            self._scale,
            self._flip_tau,
        ) = best_config

    def _fallback(self) -> None:
        self._lookback = 1
        self._w1 = 1.0
        self._w2 = 0.0
        self._bias = 0.0
        self._scale = 1.0
        self._flip_tau = 0.0

    def _build_rows(
        self, k: int, sessions: dict[int, list[Any]]
    ) -> list[tuple[float, float, float]]:
        """Build (early_ret, prev_early_ret, next_open) rows for lookback k."""
        rows: list[tuple[float, float, float]] = []
        prev_early: float | None = None

        for sid in sorted(sessions.keys()):
            sb = sessions[sid]
            if len(sb) < k + 1:
                continue

            early = sb[k - 1].close - sb[0].open
            entry = sb[k].open
            p = prev_early if prev_early is not None else 0.0
            rows.append((early, p, entry))
            prev_early = early

        return rows

    def _median_abs(self, values: list[float]) -> float:
        vals = [abs(v) for v in values if abs(v) > 1e-12]
        if not vals:
            return 1.0
        vals.sort()
        n = len(vals)
        if n % 2:
            return vals[n // 2]
        return (vals[n // 2 - 1] + vals[n // 2]) / 2.0

    def _quantile_abs(self, values: list[float], q: float) -> float:
        if q <= 0.0:
            return 0.0
        if q >= 1.0:
            return float("inf")

        vals = sorted(abs(v) for v in values)
        if not vals:
            return 0.0
        idx = int(q * len(vals))
        if idx >= len(vals):
            idx = len(vals) - 1
        return vals[idx]

    def _direction_for_z(self, z: float, fallback: int) -> int:
        if z > 0:
            return 1
        if z < 0:
            return -1
        return fallback

    def _evaluate_rows(
        self,
        zs: list[float],
        rows: list[tuple[float, float, float]],
        last_close: float,
        tau: float,
    ) -> tuple[float, int]:
        """Simulate the continuous-holding policy on training rows.

        Mirrors the runtime: first signal enters on a small/zero score using the
        full-sample drift direction; later signals flip only on a contrary score
        at least `tau` in magnitude. Entry prices are next-bar opens.
        """
        pnl = 0.0
        trades = 0
        holding = False
        desired = 0
        entry = 0.0
        drift = self._drift_direction

        for z, row in zip(zs, rows):
            entry_price = row[2]

            if not holding:
                if abs(z) >= tau:
                    desired = self._direction_for_z(z, drift)
                else:
                    desired = drift
                entry = entry_price
                holding = True
                trades += 1
                continue

            if abs(z) >= tau:
                next_desired = self._direction_for_z(z, desired)
            else:
                next_desired = desired

            if next_desired != desired:
                pnl += (entry_price - entry) * desired
                desired = next_desired
                entry = entry_price
                trades += 1

        if holding:
            pnl += (last_close - entry) * desired

        return pnl, trades

    # ------------------------------------------------------------------
    # Runtime helpers
    # ------------------------------------------------------------------
    def _apply_desired(self, ctx, desired: int) -> None:
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
        early = bar.close - first_open

        x1 = early / self._scale if self._scale else early
        x2 = self._prev_early / self._scale if self._scale else self._prev_early
        z = self._w1 * x1 + self._w2 * x2 + self._bias

        self._prev_early = early

        pos = ctx.position
        current = 1 if pos > 0 else -1 if pos < 0 else 0

        if current == 0:
            if abs(z) >= self._flip_tau:
                desired = self._direction_for_z(z, self._drift_direction)
            else:
                desired = self._drift_direction
            self._apply_desired(ctx, desired)
            return

        if abs(z) >= self._flip_tau:
            desired = self._direction_for_z(z, current)
        else:
            desired = current

        if desired != current:
            self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
