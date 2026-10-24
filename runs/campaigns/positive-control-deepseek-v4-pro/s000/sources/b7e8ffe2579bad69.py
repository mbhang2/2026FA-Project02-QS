"""Refinement of the best prior strategy family.

The best previous trials use a fitted early-session drift on 5-minute bars:
one decision per session at a fitted bar `k`, then hold across sessions with
flips only on a sufficiently strong contrary signal.  This version keeps that
exact market behavior but improves the parameter search:

  * It still fits on `ctx.train_bars("5m")` in `setup()`.
  * It selects the lookback `k`, sign convention, and flip threshold together
    by simulating the precise runtime rule -- including the no-trade handling
    for a flat early return and the flip guard -- rather than selecting `k`
    and `sign` with a simplified P&L and then setting the flip threshold as a
    side quantile.
  * Position size stays at 3, which was the best prior setting.

The runtime is deliberately conservative and close to the proven behaviour:
one 5m-bar decision per session, market orders, no stop/target, positions held
across sessions.
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Fitted early-session drift with jointly fitted flip threshold."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 3,
        max_lookback: int = 10,
        min_samples: int = 15,
        flip_quantiles: tuple = (
            0.0,
            0.1,
            0.2,
            0.25,
            0.3,
            0.4,
            0.5,
            0.6,
            0.7,
            0.8,
            0.9,
            1.0,
        ),
        trade_penalty: float = 0.0,
    ) -> None:
        self.interval = interval
        self.size = size
        self.max_lookback = max_lookback
        self.min_samples = min_samples
        self.flip_quantiles = flip_quantiles
        self.trade_penalty = trade_penalty

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

    # ------------------------------------------------------------------
    # Setup / fitting
    # ------------------------------------------------------------------
    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        self._fit(ctx)

    def _fit(self, ctx) -> None:
        """Fit lookback, sign and flip threshold from training bars."""
        try:
            bars = tuple(ctx.train_bars(self.interval))
        except Exception:
            bars = ()

        if not bars:
            # Safe fallback matching the prior default behaviour.
            return

        self._drift_direction = (
            1 if float(bars[-1].close) >= float(bars[0].close) else -1
        )
        final_close = float(bars[-1].close)

        sessions: dict[int, list[Any]] = {}
        for bar in bars:
            sessions.setdefault(bar.session, []).append(bar)

        ordered = sorted(sessions)
        if len(ordered) < self.min_samples:
            return

        min_len = min(len(sessions[sid]) for sid in ordered)
        max_k = min(self.max_lookback, min_len)
        if max_k < 1:
            return

        best_score = None
        best_trades = None
        best_k = 1
        best_sign = 1
        best_tau = 0.0

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

            tau_candidates = {0.0, float("inf")}
            for q in self.flip_quantiles:
                tau_candidates.add(self._quantile_abs(abs_returns, q))
            taus = sorted(tau_candidates)

            for sign in (1, -1):
                for flip_tau in taus:
                    pnl, trades = self._simulate(
                        rows,
                        sign=sign,
                        drift=self._drift_direction,
                        flip_tau=flip_tau,
                        final_close=final_close,
                    )
                    score = pnl - self.trade_penalty * trades

                    if (
                        best_score is None
                        or score > best_score
                        or (
                            abs(score - best_score) <= 1e-12
                            and (best_trades is None or trades < best_trades)
                        )
                    ):
                        best_score = score
                        best_trades = trades
                        best_k = k
                        best_sign = sign
                        best_tau = flip_tau

        if best_score is not None:
            self._lookback = best_k
            self._sign = best_sign
            self._flip_tau = best_tau

    def _simulate(
        self,
        rows: list[tuple[int, float, float]],
        *,
        sign: int,
        drift: int,
        flip_tau: float,
        final_close: float,
    ) -> tuple[float, int]:
        """Simulate the exact continuous-holding runtime rule."""
        pnl = 0.0
        trades = 0
        current = 0
        prev_entry: float | None = None
        eps = 1e-12

        for _sid, early_ret, entry_close in rows:
            if prev_entry is not None and current != 0:
                pnl += (entry_close - prev_entry) * current

            abs_ret = abs(early_ret)

            if abs_ret < eps:
                # Flat early return: establish drift only when flat, otherwise
                # keep the existing position.
                desired = drift if current == 0 else current
            else:
                base = sign if early_ret > 0 else -sign

                if current == 0:
                    desired = base
                elif current != base:
                    if abs_ret >= flip_tau:
                        desired = base
                    else:
                        desired = current
                else:
                    desired = current

            if desired != current:
                trades += 1
                current = desired

            prev_entry = entry_close

        if prev_entry is not None and current != 0:
            pnl += (final_close - prev_entry) * current

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
        pos = int(ctx.position)
        size = int(self.size)

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

        if len(self._session_bars) < self._lookback:
            self._session_bars.append(bar)
        self._bars_this_session += 1

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
        abs_ret = abs(early_ret)

        pos = int(ctx.position)
        current = 1 if pos > 0 else -1 if pos < 0 else 0

        if abs_ret < 1e-12:
            if current == 0:
                self._apply_desired(ctx, self._drift_direction)
            return

        base = self._sign if early_ret > 0 else -self._sign

        if current == 0:
            desired = base
            self._apply_desired(ctx, desired)
        elif current != base:
            if abs_ret >= self._flip_tau:
                self._apply_desired(ctx, base)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
