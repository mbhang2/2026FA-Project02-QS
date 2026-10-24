"""Interval-selected early-session direction, with a turnover penalty.

Builds on the strongest prior shape (#34): one position held nearly
continuously, and a fitted early-session return

    early = close(bar k) - open(bar 0)

chooses the direction.  This version adds two things:

1. The signal interval is selected from a small candidate set ("5m" and "1m")
   in `setup()` by fitting the same early-return family on training bars and
   choosing the interval/parameters with the best net continuous-holding P&L.

2. The fit objective subtracts a small penalty per trade (`fill_penalty`) so
   the optimizer prefers lower-turnover configurations when two are close in
   gross P&L.  The prior run showed that reducing fills from 48 to 38 improved
   the score, so this is a deliberate bias toward fewer fills.

The runtime is deliberately conservative: no order ids are stored, orders are
queued only when actionable and no working orders exist, and all position
changes go through the same flatten-and-reverse sizing used by the prior best.
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Fitted early-session direction, now with interval selection and a
    turnover penalty in the in-sample objective."""

    def __init__(
        self,
        *,
        intervals: tuple[str, ...] = ("5m", "1m"),
        max_lookback: int = 12,
        min_samples: int = 12,
        fill_penalty: float = 0.0015,
        flip_quantile_candidates: tuple[float, ...] = (
            0.0,
            0.25,
            0.5,
            0.75,
            1.0,
        ),
        size: int = 1,
    ) -> None:
        self.intervals = intervals
        self.max_lookback = max_lookback
        self.min_samples = min_samples
        self.fill_penalty = fill_penalty
        self.flip_quantile_candidates = flip_quantile_candidates
        self.size = size

        # Fitted in setup()
        self._interval = "5m"
        self._lookback = 1
        self._sign = 1
        self._drift_direction = 1
        self._flip_tau = 0.0

        # Per-session runtime state
        self._session: int | None = None
        self._bars_this_session = 0
        self._session_bars: list[Any] = []
        self._evaluated_this_session = False

    # ------------------------------------------------------------------
    # Setup and fitting
    # ------------------------------------------------------------------
    def setup(self, ctx) -> None:
        """Select the interval and parameters on the training split, then
        subscribe to the chosen interval."""
        best = self._fit(ctx)

        if best is None:
            # Fallback is the original placeholder shape, which is known to
            # run and to be exposed to the market.
            self._interval = "5m"
            self._lookback = 1
            self._sign = 1
            self._drift_direction = 1
            self._flip_tau = 0.0
        else:
            (
                self._interval,
                self._lookback,
                self._sign,
                self._drift_direction,
                self._flip_tau,
            ) = best

        ctx.subscribe(self._interval)

    def _fit(self, ctx):
        """Fit interval, lookback, sign, drift, and flip threshold."""
        best_net: float | None = None
        best_trades: int | None = None
        best_config = None

        for interval in self.intervals:
            try:
                bars = tuple(ctx.train_bars(interval))
            except Exception:
                continue

            if not bars:
                continue

            drift_direction = 1 if bars[-1].close >= bars[0].close else -1

            sessions: dict[int, list[Any]] = {}
            for bar in bars:
                sessions.setdefault(bar.session, []).append(bar)

            sorted_sessions = sorted(sessions.keys())
            if not sorted_sessions:
                continue

            # Final training close, used to mark the continuous position to
            # market at the end of the fitting window.
            last_close_overall = sessions[sorted_sessions[-1]][-1].close

            for k in range(1, self.max_lookback + 1):
                rows, row_last_close_map = self._build_rows(k, sessions)
                if len(rows) < self.min_samples:
                    continue

                early_abs = [abs(r[1]) for r in rows]

                for sign in (1, -1):
                    for q in self.flip_quantile_candidates:
                        tau = self._quantile_abs(early_abs, q)
                        pnl, trades = self._evaluate_rows(
                            rows,
                            last_close_overall,
                            sign,
                            tau,
                            drift_direction,
                        )
                        net = pnl - self.fill_penalty * trades

                        if best_net is None or net > best_net or (
                            abs(net - best_net) < 1e-12
                            and (best_trades is None or trades < best_trades)
                        ):
                            best_net = net
                            best_trades = trades
                            best_config = (
                                interval,
                                k,
                                sign,
                                drift_direction,
                                tau,
                            )

        return best_config

    def _build_rows(
        self, k: int, sessions: dict[int, list[Any]]
    ) -> tuple[list[tuple[int, float, float]], dict[int, float]]:
        """Build (sid, early, next_open) rows for a given lookback.

        `next_open` is the open of bar k, modelling a T+1 market fill after
        the signal bar closes.  A session must have at least k+1 bars so the
        next-bar fill exists.
        """
        rows: list[tuple[int, float, float]] = []
        last_close_map: dict[int, float] = {}

        for sid in sorted(sessions.keys()):
            sb = sessions[sid]
            if len(sb) < k + 1:
                continue
            early = sb[k - 1].close - sb[0].open
            next_open = sb[k].open
            rows.append((sid, early, next_open))
            last_close_map[sid] = sb[-1].close

        return rows, last_close_map

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

    def _direction_for_early(self, early: float, sign: int, fallback: int) -> int:
        """Desired +1/-1 for a non-zero early return, otherwise fallback."""
        if early > 1e-12:
            return sign
        if early < -1e-12:
            return -sign
        return fallback

    def _evaluate_rows(
        self,
        rows: list[tuple[int, float, float]],
        last_close_overall: float,
        sign: int,
        tau: float,
        drift_direction: int,
    ) -> tuple[float, int]:
        """Simulate continuous holding with T+1 next-bar-open fills.

        The first row always establishes exposure, mirroring the runtime's
        fallback on a flat early return.  Subsequent rows flip only when the
        contrary early return is at least `tau` in magnitude.
        """
        pnl = 0.0
        trades = 0
        holding = False
        current = 0
        entry = 0.0

        for _sid, early, next_open in rows:
            if not holding:
                desired = self._direction_for_early(
                    early, sign, drift_direction
                )
                if desired == 0:
                    desired = drift_direction
                holding = True
                current = desired
                entry = next_open
                trades += 1
                continue

            if abs(early) >= tau:
                desired = self._direction_for_early(early, sign, current)
                if desired == 0:
                    desired = current
            else:
                desired = current

            if desired != current:
                pnl += (next_open - entry) * current
                current = desired
                entry = next_open
                trades += 1

        if holding:
            pnl += (last_close_overall - entry) * current

        return pnl, trades

    # ------------------------------------------------------------------
    # Runtime helpers
    # ------------------------------------------------------------------
    def _apply_desired(self, ctx, desired: int) -> None:
        """Queue the market order needed to move from the current position to
        the desired +1/-1 position."""
        pos = ctx.position

        if desired > 0:
            if pos == 0:
                ctx.buy(size=self.size, tif="DAY", tag="session-long")
            elif pos < 0:
                ctx.buy(
                    size=-pos + self.size,
                    tif="DAY",
                    tag="session-flip-long",
                )
        elif desired < 0:
            if pos == 0:
                ctx.sell(size=self.size, tif="DAY", tag="session-short")
            elif pos > 0:
                ctx.sell(
                    size=pos + self.size,
                    tif="DAY",
                    tag="session-flip-short",
                )

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------
    def on_bars(self, ctx) -> None:
        if self._interval not in ctx.bars:
            return

        bar = ctx.bars[self._interval]

        # Reset per-session state when the session changes.
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
            desired = self._direction_for_early(
                early, self._sign, self._drift_direction
            )
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
