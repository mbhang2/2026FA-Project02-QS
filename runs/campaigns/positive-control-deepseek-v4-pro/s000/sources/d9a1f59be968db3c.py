"""Next proposal: a fitted early-session direction strategy combining two
bar-derived features: early return normalized by early range, and early
order-flow imbalance (signed volume / volume).

This keeps the validated low-turnover, once-per-session, hold-across-sessions
shape from the best prior trials (#34/#60/#61/#68), but increases the position
size from 3 to 4. The prior #60 -> #61 jump suggested modest size scaling was
not harmful and may help if the objective rewards total P&L, while the
per-contract selection objective is unchanged.
"""

from __future__ import annotations

from math import sqrt
from typing import Any


class Strategy:
    """Fitted early-session direction with a two-feature score."""

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
        size: int = 4,
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
        delta_weights: tuple[float, ...] = (
            -0.5,
            0.0,
            0.25,
            0.5,
            1.0,
        ),
        min_samples: int = 12,
        exposure_floor: float = 0.70,
    ) -> None:
        self.candidate_intervals = candidate_intervals
        self.max_lookback = max_lookback
        self.size = size
        self.entry_quantiles = entry_quantiles
        self.flip_quantiles = flip_quantiles
        self.delta_weights = delta_weights
        self.min_samples = min_samples
        self.exposure_floor = exposure_floor

        # Fitted in setup()
        self.interval = "5m"
        self._lookback = 1
        self._sign = 1
        self._drift_direction = 1
        self._entry_tau = 0.0
        self._flip_tau = 0.0
        self._delta_weight = 0.0

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

            (
                score,
                lookback,
                sign,
                drift,
                entry_tau,
                flip_tau,
                delta_weight,
            ) = fitted

            if best is None or score > best[0]:
                best = (
                    score,
                    interval,
                    lookback,
                    sign,
                    drift,
                    entry_tau,
                    flip_tau,
                    delta_weight,
                )

        if best is not None:
            (
                _,
                self.interval,
                self._lookback,
                self._sign,
                self._drift_direction,
                self._entry_tau,
                self._flip_tau,
                self._delta_weight,
            ) = best

        ctx.subscribe(self.interval)

    def _fit_interval(
        self,
        bars: tuple[Any, ...],
    ) -> tuple[float, int, int, int, float, float, float] | None:
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
            base_rows: dict[int, dict[str, float]] = {}
            for sid in ordered:
                sb = sessions[sid]
                if len(sb) < k + 1:
                    continue

                first_open = sb[0].open
                entry_close = sb[k - 1].close
                first_bars = sb[:k]
                high = max(b.high for b in first_bars)
                low = min(b.low for b in first_bars)
                rng = high - low
                ret_norm = (entry_close - first_open) / max(rng, 1e-9)

                delta_sum = sum(b.delta for b in first_bars)
                vol_sum = sum(b.volume for b in first_bars)
                delta_norm = delta_sum / max(vol_sum, 1)

                base_rows[sid] = {
                    "ret_norm": ret_norm,
                    "delta_norm": delta_norm,
                    "fill_price": sb[k].open,
                    "end_close": sb[-1].close,
                }

            if len(base_rows) < self.min_samples:
                continue

            for delta_weight in self.delta_weights:
                for sign in (1, -1):
                    rows: dict[int, tuple[float, float, float]] = {}
                    abs_scores: list[float] = []
                    for sid, info in base_rows.items():
                        raw_score = info["ret_norm"] + delta_weight * info["delta_norm"]
                        signed_score = sign * raw_score
                        rows[sid] = (
                            signed_score,
                            info["fill_price"],
                            info["end_close"],
                        )
                        abs_scores.append(abs(signed_score))

                    if len(abs_scores) < self.min_samples:
                        continue
                    abs_scores.sort()

                    for entry_q in self.entry_quantiles:
                        entry_tau = self._quantile(abs_scores, entry_q)
                        for flip_q in self.flip_quantiles:
                            flip_tau = self._quantile(abs_scores, flip_q)

                            daily_pnl, _, trades, exposed = self._simulate_path(
                                sessions,
                                ordered,
                                rows,
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
                            score = mean / (std + 1e-12)

                            if best_score is None or score > best_score:
                                best_score = score
                                best_params = (
                                    k,
                                    sign,
                                    entry_tau,
                                    flip_tau,
                                    delta_weight,
                                )

        if best_score is None or best_params is None:
            return None

        return (
            best_score,
            best_params[0],
            best_params[1],
            drift_direction,
            best_params[2],
            best_params[3],
            best_params[4],
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
        rows: dict[int, tuple[float, float, float]],
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
            start_mark = cash
            if pos != 0 and prev_close is not None:
                start_mark += pos * prev_close
            start_pos = pos

            row = rows.get(sid)
            if row is not None:
                signed_score, fill, _ = row

                if abs(signed_score) < 1e-9:
                    if pos == 0 and entry_tau <= 0:
                        desired = drift
                    else:
                        desired = 1 if pos > 0 else -1 if pos < 0 else 0
                else:
                    desired = 1 if signed_score > 0 else -1

                current = 0 if pos == 0 else (1 if pos > 0 else -1)

                if current == 0:
                    if abs(signed_score) >= entry_tau:
                        delta = desired * self.size - pos
                        if delta != 0:
                            cash -= delta * fill
                            pos = desired * self.size
                            trades += 1
                elif desired != current:
                    if abs(signed_score) >= flip_tau:
                        delta = desired * self.size - pos
                        if delta != 0:
                            cash -= delta * fill
                            pos = desired * self.size
                            trades += 1

            end_mark = cash
            if pos != 0:
                end_mark += pos * end_close
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
        """Move from the current position to desired * size contracts."""
        pos = ctx.position
        delta = desired * self.size - pos
        if delta == 0:
            return

        if delta > 0:
            ctx.buy(size=delta, tif="DAY", tag="session-direction")
        else:
            ctx.sell(size=-delta, tif="DAY", tag="session-direction")

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

        first_bars = self._session_bars[: self._lookback]
        first_open = first_bars[0].open
        entry_close = bar.close

        high = max(b.high for b in first_bars)
        low = min(b.low for b in first_bars)
        rng = high - low
        ret_norm = (entry_close - first_open) / max(rng, 1e-9)

        delta_sum = sum(b.delta for b in first_bars)
        vol_sum = sum(b.volume for b in first_bars)
        delta_norm = delta_sum / max(vol_sum, 1)

        raw_score = ret_norm + self._delta_weight * delta_norm
        signed_score = self._sign * raw_score
        pos = ctx.position

        if abs(signed_score) < 1e-9:
            if pos == 0 and self._entry_tau <= 0:
                self._apply_desired(ctx, self._drift_direction)
            return

        desired = 1 if signed_score > 0 else -1
        current = 1 if pos > 0 else -1 if pos < 0 else 0

        if current == 0:
            if abs(signed_score) >= self._entry_tau:
                self._apply_desired(ctx, desired)
        elif desired != current:
            if abs(signed_score) >= self._flip_tau:
                self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
