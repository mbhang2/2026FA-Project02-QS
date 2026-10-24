"""Refinement of the best prior family in this campaign (#61).

The winning shape has been: decide once per session from the early-session
return, hold the fitted drift across sessions, and flip only on a strong
contrary signal.  Prior trial #61 scored +0.0780 with size=3.

This version changes two things:
1. The flip threshold is no longer fixed at a quantile of absolute early
   returns.  Instead, setup() simulates the *actual* live decision rule on the
   training bars for several threshold quantiles (including "never flip") and
   picks the threshold, lookback, and sign that maximise continuous holding
   P&L while favouring lower turnover on ties.
2. Position size is raised to 4, on the evidence that the fitted per-contract
   edge survived the move from size 2 (#60) to size 3 (#61).

The rest is deliberately conservative: no stop/target bracket, low turnover,
and one decision point per session.
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Fitted early-session drift, with threshold selection and size 4."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 4,
        max_lookback: int = 12,
        flip_quantiles: tuple[float, ...] = (
            0.25,
            0.35,
            0.50,
            0.65,
            0.80,
            1.0,
        ),
        min_samples: int = 12,
    ) -> None:
        self.interval = interval
        self.size = size
        self.max_lookback = max_lookback
        self.flip_quantiles = flip_quantiles
        self.min_samples = min_samples

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
        """Fit lookback, sign, drift, and the live flip threshold."""
        try:
            bars = tuple(ctx.train_bars(self.interval))
        except Exception:
            bars = ()

        if bars:
            self._drift_direction = 1 if bars[-1].close >= bars[0].close else -1
        else:
            self._drift_direction = 1

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

        best_score = None
        best_k = 1
        best_sign = 1
        best_tau = 0.0
        best_trades = None

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

            for sign in (1, -1):
                candidates: list[tuple[str, float]] = []
                seen: set[float] = set()

                for q in self.flip_quantiles:
                    tau = self._quantile_abs(abs_returns, q)
                    if tau not in seen:
                        candidates.append((f"q{q}", tau))
                        seen.add(tau)

                # Also consider never flipping.  This is the extreme low-turnover case.
                candidates.append(("never", float("inf")))

                for _name, tau in candidates:
                    pnl, trades = self._simulate(
                        rows, sign, tau, self._drift_direction, final_close
                    )
                    if (
                        best_score is None
                        or pnl > best_score
                        or (abs(pnl - best_score) <= 1e-12 and (best_trades is None or trades < best_trades))
                    ):
                        best_score = pnl
                        best_k = k
                        best_sign = sign
                        best_tau = tau
                        best_trades = trades

        if best_score is not None:
            self._lookback = best_k
            self._sign = best_sign
            self._flip_tau = best_tau

    def _simulate(
        self,
        rows: list[tuple[int, float, float]],
        sign: int,
        tau: float,
        drift_direction: int,
        final_close: float,
    ) -> tuple[float, int]:
        """Simulate the actual live decision rule on training rows."""
        pnl = 0.0
        trades = 0
        current = 0
        entry: float | None = None

        for _sid, early_ret, close_k in rows:
            # Mark to market from the previous decision point to this one.
            if current != 0 and entry is not None:
                pnl += (close_k - entry) * current
                entry = close_k

            # Zero early return is handled exactly as in live: it only enters
            # when flat; it never flips an existing position.
            if abs(early_ret) < 1e-12:
                if current == 0 and drift_direction != 0:
                    current = drift_direction
                    entry = close_k
                    trades += 1
                continue

            desired = sign if early_ret > 0 else -sign

            if current == 0:
                if desired != 0:
                    current = desired
                    entry = close_k
                    trades += 1
            elif current != desired:
                if abs(early_ret) >= tau:
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
    def _desired_for_early_ret(self, early_ret: float) -> int:
        if early_ret > 0:
            return self._sign
        if early_ret < 0:
            return -self._sign
        return 0

    def _apply_desired(self, ctx, desired: int) -> None:
        """Queue the market order needed to move to the desired position."""
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

        pos = ctx.position
        current = 1 if pos > 0 else -1 if pos < 0 else 0

        # Flat early return: only establish exposure if currently flat.
        if abs(early_ret) < 1e-12:
            if current == 0:
                self._apply_desired(ctx, self._drift_direction)
            return

        desired = self._desired_for_early_ret(early_ret)

        if current == 0:
            self._apply_desired(ctx, desired)
        elif desired != current:
            # Only flip on a strong contrary signal; small signals are likely
            # noise and turnover has real cost.
            if abs(early_ret) >= self._flip_tau:
                self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
