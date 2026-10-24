"""A refined early-session drift strategy.

This is the next generation of the best prior family in this campaign (#61).
The core remains: decide once per session from the early-session return, hold
with low turnover, and flip only on a strong contrary signal.

The key change is that the signal is normalised by the early-session range and
blended with the previous session's normalised full-session return.  The
lookback, blend coefficient, sign, and flip threshold are all fitted in setup()
from training bars by simulating the actual live decision rule and maximising
continuous-holding P&L, with lower turnover used as a tie-breaker.

This keeps the successful low-frequency shape while letting the data decide
whether multi-session momentum helps filter flips.
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Fitted early-session direction with range normalisation and session context."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 3,
        max_lookback: int = 8,
        gamma_grid: tuple[float, ...] = (-1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5),
        tau_quantiles: tuple[float, ...] = (0.0, 0.25, 0.50, 0.75, 1.0),
        min_samples: int = 10,
    ) -> None:
        self.interval = interval
        self.size = size
        self.max_lookback = max_lookback
        self.gamma_grid = gamma_grid
        self.tau_quantiles = tau_quantiles
        self.min_samples = min_samples

        # Fitted in setup()
        self._lookback = 1
        self._gamma = 0.0
        self._flip_sign = 1
        self._tau = float("inf")
        self._drift_direction = 1

        # Per-session runtime state
        self._session: int | None = None
        self._bars_this_session = 0
        self._session_bars: list[Any] = []
        self._evaluated_this_session = False
        self._prev_full_ret_norm = 0.0

    # ------------------------------------------------------------------
    # Setup / fitting
    # ------------------------------------------------------------------
    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        self._fit(ctx)

    def _fit(self, ctx) -> None:
        """Fit lookback, blend, sign, and flip threshold from training bars."""
        try:
            bars = tuple(ctx.train_bars(self.interval))
        except Exception:
            bars = ()

        if not bars:
            return

        self._drift_direction = 1 if float(bars[-1].close) >= float(bars[0].close) else -1

        sessions: dict[int, list[Any]] = {}
        for bar in bars:
            sessions.setdefault(bar.session, []).append(bar)

        ordered = sorted(sessions)
        if len(ordered) < self.min_samples:
            return

        # Previous session's full-session normalised return, used as context.
        full_ret_norm: dict[int, float] = {}
        for sid in ordered:
            sb = sessions[sid]
            if not sb:
                full_ret_norm[sid] = 0.0
                continue
            first_open = float(sb[0].open)
            last_close = float(sb[-1].close)
            h = max(float(b.high) for b in sb)
            l = min(float(b.low) for b in sb)
            rng = h - l
            full_ret_norm[sid] = (last_close - first_open) / rng if rng > 1e-12 else 0.0

        prev_sid: dict[int, int] = {}
        for i in range(1, len(ordered)):
            prev_sid[ordered[i]] = ordered[i - 1]

        max_k = min(
            self.max_lookback,
            min(len(sessions[s]) for s in ordered),
        )
        if max_k < 1:
            return

        final_close = float(bars[-1].close)

        best_score: float | None = None
        best_trades: int | None = None
        best_k = 1
        best_gamma = 0.0
        best_sign = 1
        best_tau = float("inf")

        for k in range(1, max_k + 1):
            for gamma in self.gamma_grid:
                for flip_sign in (1, -1):
                    rows: list[tuple[int, float, float]] = []

                    for sid in ordered:
                        sb = sessions[sid]
                        if len(sb) < k:
                            continue

                        first_k = sb[:k]
                        first_open = float(first_k[0].open)
                        close_k = float(first_k[-1].close)
                        h = max(float(b.high) for b in first_k)
                        l = min(float(b.low) for b in first_k)
                        rng = h - l
                        early_norm = (
                            (close_k - first_open) / rng if rng > 1e-12 else 0.0
                        )

                        context = full_ret_norm.get(prev_sid.get(sid, -1), 0.0)
                        raw = early_norm + gamma * context
                        signal = flip_sign * raw

                        rows.append((sid, signal, close_k))

                    if len(rows) < self.min_samples:
                        continue

                    abs_signals = [abs(r[1]) for r in rows]
                    tau_candidates = {float("inf")}
                    for q in self.tau_quantiles:
                        tau_candidates.add(self._quantile_abs(abs_signals, q))

                    for tau in tau_candidates:
                        pnl, trades = self._simulate_rows(rows, tau, final_close)

                        if best_score is None or pnl > best_score or (
                            abs(pnl - best_score) <= 1e-12
                            and (best_trades is None or trades < best_trades)
                        ):
                            best_score = pnl
                            best_trades = trades
                            best_k = k
                            best_gamma = gamma
                            best_sign = flip_sign
                            best_tau = tau

        if best_score is not None:
            self._lookback = best_k
            self._gamma = best_gamma
            self._flip_sign = best_sign
            self._tau = best_tau

    def _simulate_rows(
        self,
        rows: list[tuple[int, float, float]],
        tau: float,
        final_close: float,
    ) -> tuple[float, int]:
        """Simulate the live continuous-holding decision rule on training rows."""
        pnl = 0.0
        trades = 0
        current = 0
        entry: float | None = None

        for _sid, signal, close_k in rows:
            # Mark to market from the previous decision point to this one.
            if current != 0 and entry is not None:
                pnl += (close_k - entry) * current
                entry = close_k

            # A perfectly flat signal is handled exactly as live: it only
            # establishes exposure when flat, never flips an existing position.
            if abs(signal) < 1e-12:
                if current == 0:
                    current = self._drift_direction
                    entry = close_k
                    trades += 1
                continue

            desired = 1 if signal > 0 else -1

            if current == 0:
                current = desired
                entry = close_k
                trades += 1
            elif current != desired:
                if abs(signal) >= tau:
                    current = desired
                    entry = close_k
                    trades += 1

        if current != 0 and entry is not None:
            pnl += (final_close - entry) * current

        return pnl, trades

    def _quantile_abs(self, values: list[float], q: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = int(q * len(ordered))
        if idx >= len(ordered):
            idx = len(ordered) - 1
        return float(ordered[idx])

    # ------------------------------------------------------------------
    # Runtime
    # ------------------------------------------------------------------
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

        # Session changed: finalise previous session's full-session context,
        # then reset per-session state.
        if ctx.session != self._session:
            if self._session is not None and self._session_bars:
                prev = self._session_bars
                first_open = float(prev[0].open)
                last_close = float(prev[-1].close)
                h = max(float(b.high) for b in prev)
                l = min(float(b.low) for b in prev)
                rng = h - l
                self._prev_full_ret_norm = (
                    (last_close - first_open) / rng if rng > 1e-12 else 0.0
                )
            else:
                self._prev_full_ret_norm = 0.0

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
        if self._evaluated_this_session or self._bars_this_session != self._lookback:
            return

        self._evaluated_this_session = True

        first_k = self._session_bars[: self._lookback]
        if len(first_k) < self._lookback:
            return

        first_open = float(first_k[0].open)
        close_now = float(bar.close)
        h = max(float(b.high) for b in first_k)
        l = min(float(b.low) for b in first_k)
        rng = h - l
        early_norm = (close_now - first_open) / rng if rng > 1e-12 else 0.0

        raw = early_norm + self._gamma * self._prev_full_ret_norm
        signal = self._flip_sign * raw

        pos = ctx.position
        current = 1 if pos > 0 else -1 if pos < 0 else 0

        if abs(signal) < 1e-12:
            if current == 0:
                self._apply_desired(ctx, self._drift_direction)
            return

        desired = 1 if signal > 0 else -1

        if current == 0:
            self._apply_desired(ctx, desired)
        elif current != desired:
            if abs(signal) >= self._tau:
                self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
