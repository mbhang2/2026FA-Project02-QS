"""Fitted early-session direction on 30-second bars.

The best prior shape (#34) used 5-minute bars, which are very coarse on this
synthetic dataset.  The same family of signal is implemented here on 30-second
bars: after a fitted lookback `k`, compute

    early = close(bar k) - open(bar 0)

and choose a position from sign(early) * fitted_sign, holding continuously and
flipping only when a contrary early return is large relative to a fitted
threshold.

Fitting mirrors the engine's T+1 rule by assuming the entry/flip fill occurs at
the next bar's open.  The lookback, sign convention, and flip threshold are
jointly chosen on `ctx.train_bars("30s")` in `setup()`.
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Fitted early-session direction on 30-second bars."""

    def __init__(
        self,
        *,
        interval: str = "30s",
        size: int = 1,
        max_lookback: int = 20,
        min_samples: int = 15,
        fill_penalty: float = 1e-6,
        flip_quantile_candidates: tuple[float, ...] = (0.0, 0.25, 0.5, 0.75),
    ) -> None:
        self.interval = interval
        self.size = size
        self.max_lookback = max_lookback
        self.min_samples = min_samples
        self.fill_penalty = fill_penalty
        self.flip_quantile_candidates = flip_quantile_candidates

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

    # ------------------------------------------------------------------
    # Setup / fitting
    # ------------------------------------------------------------------
    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        self._fit(ctx)

    def _fit(self, ctx) -> None:
        """Fit lookback, sign, and flip threshold from training 30s bars."""
        try:
            bars = tuple(ctx.train_bars(self.interval))
        except Exception:
            bars = ()

        if bars:
            self._drift_direction = 1 if bars[-1].close >= bars[0].close else -1
        else:
            self._drift_direction = 1
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
        best_config: tuple[int, int, float] | None = None

        for k in range(1, self.max_lookback + 1):
            rows = self._build_rows(k, sessions)
            if len(rows) < self.min_samples:
                continue

            early_abs = [abs(r[0]) for r in rows]

            for sign in (1, -1):
                for q in self.flip_quantile_candidates:
                    tau = self._quantile(early_abs, q)
                    gross, trades = self._evaluate_rows(rows, last_close, sign, tau)
                    net = gross - self.fill_penalty * trades

                    if best_net is None or net > best_net:
                        best_net = net
                        best_config = (k, sign, tau)

        if best_config is None:
            self._fallback()
            return

        self._lookback, self._sign, self._flip_tau = best_config

    def _fallback(self) -> None:
        self._lookback = 1
        self._sign = 1
        self._drift_direction = 1
        self._flip_tau = 0.0

    def _build_rows(
        self, k: int, sessions: dict[int, list[Any]]
    ) -> list[tuple[float, float]]:
        """Build (early, next_open) rows for a given lookback.

        Requires k+1 bars so there is a next bar at whose open the T+1 fill
        would occur.
        """
        rows: list[tuple[float, float]] = []
        for sid in sorted(sessions.keys()):
            sb = sessions[sid]
            if len(sb) < k + 1:
                continue
            early = sb[k - 1].close - sb[0].open
            next_open = sb[k].open
            rows.append((early, next_open))
        return rows

    def _quantile(self, values: list[float], q: float) -> float:
        if q <= 0.0:
            return 0.0
        if q >= 1.0:
            return float("inf")

        vals = sorted(values)
        if not vals:
            return 0.0
        idx = int(q * len(vals))
        if idx >= len(vals):
            idx = len(vals) - 1
        return vals[idx]

    def _direction_for_early(self, early: float, sign: int, fallback: int) -> int:
        if early > 1e-12:
            return sign
        if early < -1e-12:
            return -sign
        return fallback

    def _evaluate_rows(
        self,
        rows: list[tuple[float, float]],
        last_close: float,
        sign: int,
        tau: float,
    ) -> tuple[float, int]:
        """Simulate continuous holding with T+1 next-bar-open fills.

        Mirrors the runtime: first row always establishes exposure; subsequent
        rows flip only when a contrary signal is at least `tau` in magnitude.
        A flip is modelled as one fill at the next bar's open, matching the
        one-order flatten-and-reverse used by `_apply_desired`.
        """
        pnl = 0.0
        trades = 0
        holding = False
        desired = 0
        entry = 0.0

        for early, next_open in rows:
            if not holding:
                desired = self._direction_for_early(
                    early, sign, self._drift_direction
                )
                entry = next_open
                holding = True
                trades += 1
                continue

            if abs(early) >= tau:
                new_desired = self._direction_for_early(early, sign, desired)
            else:
                new_desired = desired

            if new_desired != desired:
                pnl += (next_open - entry) * desired
                desired = new_desired
                entry = next_open
                trades += 1

        if holding:
            pnl += (last_close - entry) * desired

        return pnl, trades

    # ------------------------------------------------------------------
    # Runtime helpers
    # ------------------------------------------------------------------
    def _apply_desired(self, ctx, desired: int) -> None:
        """Queue the market order needed to move from position to desired."""
        pos = ctx.position

        if desired > 0:
            if pos == 0:
                ctx.buy(size=self.size, tif="DAY", tag="session-long")
            elif pos < 0:
                ctx.buy(size=-pos + self.size, tif="DAY", tag="session-flip-long")
        elif desired < 0:
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

        # Session changed: reset per-session state.
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

        pos = ctx.position
        current = 1 if pos > 0 else -1 if pos < 0 else 0

        if current == 0:
            if abs(early) >= self._flip_tau:
                desired = self._direction_for_early(
                    early, self._sign, self._drift_direction
                )
            else:
                desired = self._drift_direction
            self._apply_desired(ctx, desired)
            return

        if abs(early) >= self._flip_tau:
            desired = self._direction_for_early(early, self._sign, current)
        else:
            desired = current

        if desired != current:
            self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
