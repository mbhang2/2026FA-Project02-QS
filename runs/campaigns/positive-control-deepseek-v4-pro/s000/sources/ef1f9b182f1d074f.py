"""Fitted early-session drift with separate entry and flip thresholds.

The winning prior family decides once per session from the early-session return,
holds across sessions, and flips only on a sufficiently strong contrary signal.
This version keeps that exact low-frequency shape but adds two refinements:

1. A separate *entry* threshold.  When flat, a very small early return is treated
   as uninformative and the strategy instead takes the full-sample drift
   direction, rather than the sign of a noisy tick move.
2. The flip threshold and the entry threshold are both selected in setup() by
   simulating the live continuous-holding rule on training 5-minute bars.

The rest is deliberately conservative: one decision per session, no intra-session
stops, size 3, and day-tif orders.  Runtime uses only closed bars and never reads
beyond the current instant.
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Fitted early-session drift with an entry-confidence filter."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 3,
        max_lookback: int = 8,
        entry_quantiles: tuple[float, ...] = (
            0.0,
            0.10,
            0.20,
            0.25,
            0.30,
            0.40,
            0.50,
        ),
        flip_quantiles: tuple[float, ...] = (
            0.25,
            0.40,
            0.55,
            0.70,
            0.80,
            0.90,
            1.00,
        ),
        min_samples: int = 10,
    ) -> None:
        self.interval = interval
        self.size = size
        self.max_lookback = max_lookback
        self.entry_quantiles = entry_quantiles
        self.flip_quantiles = flip_quantiles
        self.min_samples = min_samples

        # Fitted in setup()
        self._lookback = 1
        self._sign = 1
        self._drift_direction = 1
        self._entry_tau = 0.0
        self._flip_tau = 0.0

        # Per-session runtime state
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
        """Fit lookback, sign, entry threshold and flip threshold."""
        try:
            bars = tuple(ctx.train_bars(self.interval))
        except Exception:
            bars = ()

        if not bars:
            # Safe fallback: trade the early-session sign with no entry filter.
            self._lookback = 1
            self._sign = 1
            self._drift_direction = 1
            self._entry_tau = 0.0
            self._flip_tau = 0.0
            return

        self._drift_direction = 1 if float(bars[-1].close) >= float(bars[0].close) else -1

        sessions: dict[int, list[Any]] = {}
        for bar in bars:
            sessions.setdefault(bar.session, []).append(bar)

        ordered = sorted(sessions.keys())
        if len(ordered) < self.min_samples:
            return

        max_k = min(self.max_lookback, min(len(sessions[s]) for s in ordered))
        if max_k < 1:
            return

        final_close = float(bars[-1].close)

        best_score: float | None = None
        best_trades: int | None = None
        best_k = 1
        best_sign = 1
        best_entry_tau = 0.0
        best_flip_tau = 0.0

        for k in range(1, max_k + 1):
            rows: list[tuple[int, float, float]] = []
            abs_returns: list[float] = []

            for sid in ordered:
                sb = sessions[sid]
                if len(sb) < k:
                    continue
                first_open = float(sb[0].open)
                close_k = float(sb[k - 1].close)
                early_ret = close_k - first_open
                rows.append((sid, early_ret, close_k))
                abs_returns.append(abs(early_ret))

            if len(rows) < self.min_samples:
                continue

            entry_candidates = sorted(
                {self._quantile_abs(abs_returns, q) for q in self.entry_quantiles}
            )
            flip_candidates = sorted(
                {self._quantile_abs(abs_returns, q) for q in self.flip_quantiles}
                | {float("inf")}
            )

            for sign in (1, -1):
                for entry_tau in entry_candidates:
                    for flip_tau in flip_candidates:
                        if entry_tau > flip_tau:
                            continue

                        pnl, trades = self._simulate(
                            rows,
                            sign=sign,
                            entry_tau=entry_tau,
                            flip_tau=flip_tau,
                            drift=self._drift_direction,
                            final_close=final_close,
                        )

                        if (
                            best_score is None
                            or pnl > best_score
                            or (
                                abs(pnl - best_score) <= 1e-12
                                and (best_trades is None or trades < best_trades)
                            )
                        ):
                            best_score = pnl
                            best_trades = trades
                            best_k = k
                            best_sign = sign
                            best_entry_tau = entry_tau
                            best_flip_tau = flip_tau

        if best_score is not None:
            self._lookback = best_k
            self._sign = best_sign
            self._entry_tau = best_entry_tau
            self._flip_tau = best_flip_tau

    def _simulate(
        self,
        rows: list[tuple[int, float, float]],
        *,
        sign: int,
        entry_tau: float,
        flip_tau: float,
        drift: int,
        final_close: float,
    ) -> tuple[float, int]:
        """Simulate the actual live decision rule on training rows."""
        pnl = 0.0
        trades = 0
        current = 0
        prev_close: float | None = None

        for _sid, early_ret, close_k in rows:
            # Mark to market from the previous decision point to this one.
            if prev_close is not None and current != 0:
                pnl += (close_k - prev_close) * current
            prev_close = close_k

            abs_ret = abs(early_ret)

            # Exactly zero early return is handled like live: it only enters
            # the drift when flat, never flips an existing position.
            if abs_ret < 1e-12:
                if current == 0:
                    current = drift
                    trades += 1
                continue

            base = sign if early_ret > 0 else -sign

            if current == 0:
                # Entry filter: only trust the early-return sign if it was large
                # enough; otherwise fall back to the full-sample drift.
                target = base if abs_ret >= entry_tau else drift
                if target != 0:
                    current = target
                    trades += 1
            elif current != base:
                # Flip only on a strong contrary early-session signal.
                if abs_ret >= flip_tau:
                    current = base
                    trades += 1

        if prev_close is not None and current != 0:
            pnl += (final_close - prev_close) * current

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
        if self._evaluated_this_session or self._bars_this_session != self._lookback:
            return

        self._evaluated_this_session = True

        first_open = float(self._session_bars[0].open)
        early_ret = float(bar.close) - first_open
        abs_ret = abs(early_ret)

        pos = ctx.position
        current = 1 if pos > 0 else -1 if pos < 0 else 0

        # Zero early return: only establish exposure when flat; never flip.
        if abs_ret < 1e-12:
            if current == 0:
                self._apply_desired(ctx, self._drift_direction)
            return

        base = self._sign if early_ret > 0 else -self._sign

        if current == 0:
            desired = base if abs_ret >= self._entry_tau else self._drift_direction
            self._apply_desired(ctx, desired)
        elif current != base:
            if abs_ret >= self._flip_tau:
                self._apply_desired(ctx, base)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
