"""Enhanced version of the best prior campaign strategy (#66): same fitted
session-direction model, but with position size increased from 3 to 4.

The best previous scores in this campaign plateaued at +0.0780 with size=3
and 59 trades.  The edge appears to be per-contract and persistent, so
scaling the position size while keeping the exact same fitted parameters and
low-turnover, one-decision-per-session logic should increase net P&L without
changing the number of trades.

Strategy shape (unchanged from #66, which was itself a refinement of #61):
- Fit interval, lookback k, sign, drift direction, entry threshold, and flip
  threshold from training bars using a Sharpe-like objective over simulated
  session mark-to-market P&L.
- In live trading, evaluate once per session at bar k:
  - If flat, establish position in the drifted direction if the early return
    clears the entry threshold.
  - If already positioned, flip only when a strong opposite signal clears the
    flip threshold.
- Hold across sessions with no stop or target; this minimises commission and
  matches the proven profitable behaviour.
"""

from __future__ import annotations

from math import sqrt
from typing import Any


class Strategy:
    """Fitted early-session direction with entry/flip thresholds, size=4."""

    def __init__(
        self,
        *,
        candidate_intervals: tuple[str, ...] = (
            "1m",
            "2m",
            "3m",
            "5m",
            "10m",
            "15m",
            "30m",
        ),
        max_lookback: int = 16,
        size: int = 4,                       # increased from 3 to 4
        entry_quantiles: tuple[float, ...] = (
            0.0,
            0.1,
            0.2,
            0.3,
            0.4,
            0.5,
        ),
        flip_quantiles: tuple[float, ...] = (
            0.1,
            0.2,
            0.3,
            0.4,
            0.5,
            0.6,
            0.7,
            0.8,
        ),
        min_samples: int = 12,
        exposure_floor: float = 0.70,
    ) -> None:
        self.candidate_intervals = candidate_intervals
        self.max_lookback = max_lookback
        self.size = size
        self.entry_quantiles = entry_quantiles
        self.flip_quantiles = flip_quantiles
        self.min_samples = min_samples
        self.exposure_floor = exposure_floor

        # Fitted in setup()
        self.interval = "5m"
        self._lookback = 1
        self._sign = 1
        self._drift_direction = 1
        self._entry_tau = 0.0
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

        for interval in self.candidate_intervals:
            try:
                bars = tuple(ctx.train_bars(interval))
            except Exception:
                continue
            if not bars:
                continue

            fitted = self._fit_interval(bars)
            if fitted is None:
                continue

            score, lookback, sign, drift, entry_tau, flip_tau = fitted
            if best is None or score > best[0]:
                best = (score, interval, lookback, sign, drift, entry_tau, flip_tau)

        if best is not None:
            (
                _,
                self.interval,
                self._lookback,
                self._sign,
                self._drift_direction,
                self._entry_tau,
                self._flip_tau,
            ) = best

        ctx.subscribe(self.interval)

    def _fit_interval(
        self,
        bars: tuple[Any, ...],
    ) -> tuple[float, int, int, int, float, float] | None:
        sessions: dict[int, list[Any]] = {}
        for bar in bars:
            sessions.setdefault(bar.session, []).append(bar)
        ordered = sorted(sessions)

        if len(ordered) < self.min_samples:
            return None

        drift_direction = 1 if bars[-1].close >= bars[0].close else -1

        best_score: float | None = None
        best_params = None

        for k in range(1, self.max_lookback + 1):
            abs_returns: list[float] = []
            for sid in ordered:
                sb = sessions[sid]
                if len(sb) < k:
                    continue
                early_ret = sb[k - 1].close - sb[0].open
                if abs(early_ret) > 1e-12:
                    abs_returns.append(abs(early_ret))

            if len(abs_returns) < self.min_samples:
                continue
            abs_returns.sort()

            for entry_q in self.entry_quantiles:
                entry_tau = self._quantile(abs_returns, entry_q)
                for flip_q in self.flip_quantiles:
                    flip_tau = self._quantile(abs_returns, flip_q)
                    for sign in (1, -1):
                        daily_pnl, _, trades, exposed = self._simulate_path(
                            sessions,
                            ordered,
                            k,
                            sign,
                            drift_direction,
                            entry_tau,
                            flip_tau,
                        )

                        if trades < 3 or trades > 150:
                            continue
                        if exposed < self.exposure_floor * len(ordered):
                            continue
                        if len(daily_pnl) < 3:
                            continue

                        mean = sum(daily_pnl) / len(daily_pnl)
                        variance = (
                            sum((x - mean) ** 2 for x in daily_pnl)
                            / len(daily_pnl)
                        )
                        std = sqrt(max(variance, 0.0))
                        # Sharpe-like objective.  Prefer smooth per-session P&L.
                        score = mean / (std + 1e-12)

                        if best_score is None or score > best_score:
                            best_score = score
                            best_params = (k, sign, entry_tau, flip_tau)

        if best_score is None or best_params is None:
            return None

        return (
            best_score,
            best_params[0],
            best_params[1],
            drift_direction,
            best_params[2],
            best_params[3],
        )

    def _quantile(self, values: list[float], q: float) -> float:
        if not values:
            return 0.0
        idx = int(q * (len(values) - 1))
        if idx < 0:
            idx = 0
        if idx >= len(values):
            idx = len(values) - 1
        return values[idx]

    def _simulate_path(
        self,
        sessions: dict[int, list[Any]],
        ordered: list[int],
        k: int,
        sign: int,
        drift: int,
        entry_tau: float,
        flip_tau: float,
    ) -> tuple[list[float], float, int, int]:
        cash = 0.0
        pos = 0
        prev_close: float | None = None
        daily_pnl: list[float] = []
        trades = 0
        exposed = 0

        for sid in ordered:
            sb = sessions.get(sid, [])
            if not sb:
                daily_pnl.append(0.0)
                continue

            end_close = sb[-1].close
            start_mark = cash + (0.0 if pos == 0 else pos * prev_close if prev_close is not None else 0.0)
            start_pos = pos

            if len(sb) >= k:
                first_open = sb[0].open
                entry_close = sb[k - 1].close
                early_ret = entry_close - first_open

                desired = drift
                if abs(early_ret) > 1e-12:
                    desired = sign if early_ret > 0 else -sign

                current = 0 if pos == 0 else (1 if pos > 0 else -1)

                fill = sb[k].open if k < len(sb) else entry_close

                if abs(early_ret) < 1e-12:
                    if current == 0 and entry_tau <= 0:
                        cash -= desired * self.size * fill
                        pos = desired * self.size
                        trades += 1
                elif current == 0:
                    if abs(early_ret) >= entry_tau:
                        cash -= desired * self.size * fill
                        pos = desired * self.size
                        trades += 1
                elif desired != current:
                    if abs(early_ret) >= flip_tau:
                        delta = desired * self.size - pos
                        cash -= delta * fill
                        pos = desired * self.size
                        trades += 1

            end_mark = cash + (0.0 if pos == 0 else pos * end_close)
            pnl = end_mark - start_mark
            daily_pnl.append(pnl)

            if start_pos != 0 or pos != 0:
                exposed += 1

            prev_close = end_close

        final_pnl = end_mark
        return daily_pnl, final_pnl, trades, exposed

    # ------------------------------------------------------------------
    # Live callbacks
    # ------------------------------------------------------------------
    def _apply_desired(self, ctx, desired: int) -> None:
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

        if abs(early_ret) < 1e-12:
            if pos == 0 and self._entry_tau <= 0:
                self._apply_desired(ctx, self._drift_direction)
            return

        desired = self._sign if early_ret > 0 else -self._sign
        current = 1 if pos > 0 else -1 if pos < 0 else 0

        if current == 0:
            if abs(early_ret) >= self._entry_tau:
                self._apply_desired(ctx, desired)
        elif desired != current:
            if abs(early_ret) >= self._flip_tau:
                self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
