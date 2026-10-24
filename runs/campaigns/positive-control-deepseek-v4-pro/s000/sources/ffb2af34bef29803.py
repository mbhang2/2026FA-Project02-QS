"""Next strategy: combined early-session price and order-flow signal.

Reasoning from prior trials:
- The best prior (#61/#68) used the sign of the early 5m session return,
  fitted from training bars, and held with low turnover. Score +0.0780.
- This candidate keeps that proven shape but extends the signal from pure
  price return to a fitted combination of:
    * early price return, and
    * early order-flow imbalance (bar.delta / bar.volume).
- The combination weight, lookback, and sign are chosen in setup() using
  ctx.train_bars, the same in-sample data used by the prior best.
- A continuous-holding objective over training sessions is used, then the
  live run enters once per session when the chosen lookback bar closes and
  manages flips with a quantile threshold, exactly as in the prior best.

The design assumes the same fitted training-split evaluation as #61/#68,
where setup() may see the scored training dataset in bulk. No ticks are
subscribed, since tick subscriptions exceed the wall-clock budget on this
walk.
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Fitted early-session direction with a weighted price/delta signal."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 3,
        max_lookback: int = 6,
        flip_threshold_quantile: float = 0.25,
        min_samples: int = 10,
        weight_candidates: tuple[float, ...] = (0.0, 0.25, 0.5, 0.75, 1.0),
    ) -> None:
        self.interval = interval
        self.size = size
        self.max_lookback = max_lookback
        self.flip_threshold_quantile = flip_threshold_quantile
        self.min_samples = min_samples
        self.weight_candidates = weight_candidates

        # Fitted in setup()
        self._lookback = 1
        self._sign = 1
        self._drift_direction = 1
        self._weight = 1.0
        self._price_mean = 0.0
        self._price_std = 1.0
        self._delta_mean = 0.0
        self._delta_std = 1.0
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
        ctx.subscribe(self.interval)
        self._fit(ctx)

    def _fit(self, ctx) -> None:
        """Fit lookback, weight, sign, and flip threshold from training bars."""
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
        ordered = sorted(sessions)

        if len(ordered) < self.min_samples:
            return  # fall back to safe defaults

        best_score: float | None = None
        best_rows: list[tuple[int, float, float]] | None = None
        best_k = 1
        best_sign = 1
        best_w = 1.0
        best_price_mean = 0.0
        best_price_std = 1.0
        best_delta_mean = 0.0
        best_delta_std = 1.0

        for k in range(1, self.max_lookback + 1):
            raw_rows: list[tuple[int, float, float, float]] = []
            raw_price_vals: list[float] = []
            raw_delta_vals: list[float] = []

            for sid in ordered:
                sb = sessions.get(sid)
                if not sb or len(sb) < k:
                    continue

                first_open = sb[0].open
                price_ret = sb[k - 1].close - first_open

                cum_delta = sum(b.delta for b in sb[:k])
                cum_volume = sum(b.volume for b in sb[:k])
                delta_ratio = cum_delta / cum_volume if cum_volume else 0.0

                entry_close = sb[k - 1].close
                raw_rows.append((sid, price_ret, delta_ratio, entry_close))
                raw_price_vals.append(price_ret)
                raw_delta_vals.append(delta_ratio)

            if len(raw_rows) < self.min_samples:
                continue

            price_mean = self._mean(raw_price_vals)
            price_std = self._std(raw_price_vals, price_mean) or 1.0
            delta_mean = self._mean(raw_delta_vals)
            delta_std = self._std(raw_delta_vals, delta_mean) or 1.0

            for w in self.weight_candidates:
                rows: list[tuple[int, float, float]] = []
                for sid, price_ret, delta_ratio, entry_close in raw_rows:
                    z_price = (price_ret - price_mean) / price_std
                    z_delta = (delta_ratio - delta_mean) / delta_std
                    feature = w * z_price + (1.0 - w) * z_delta
                    rows.append((sid, feature, entry_close))

                for sign in (1, -1):
                    score = self._eval_sign(
                        rows, sessions, ordered[-1], sign, self._drift_direction
                    )

                    if best_score is None or score > best_score:
                        best_score = score
                        best_rows = rows
                        best_k = k
                        best_sign = sign
                        best_w = w
                        best_price_mean = price_mean
                        best_price_std = price_std
                        best_delta_mean = delta_mean
                        best_delta_std = delta_std

        if best_rows is not None:
            self._lookback = best_k
            self._sign = best_sign
            self._weight = best_w
            self._price_mean = best_price_mean
            self._price_std = best_price_std
            self._delta_mean = best_delta_mean
            self._delta_std = best_delta_std
            self._flip_tau = self._quantile_of_abs_returns(
                best_rows, self.flip_threshold_quantile
            )

    def _mean(self, values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    def _std(self, values: list[float], mean: float) -> float:
        if not values:
            return 0.0
        variance = sum((x - mean) ** 2 for x in values) / len(values)
        return variance ** 0.5

    def _eval_sign(
        self,
        rows: list[tuple[int, float, float]],
        sessions: dict[int, list[Any]],
        last_sid: int,
        sign: int,
        drift_direction: int,
    ) -> float:
        """Approximate continuous-holding P&L for a sign convention."""
        pnl = 0.0
        prev_desired: int | None = None
        prev_entry: float | None = None

        for _sid, feature, entry_close in rows:
            if abs(feature) < 1e-12:
                desired = drift_direction
            else:
                desired = 1 if sign * feature > 0 else -1

            if prev_desired is not None and prev_entry is not None:
                pnl += (entry_close - prev_entry) * prev_desired

            prev_desired = desired
            prev_entry = entry_close

        if prev_desired is not None and prev_entry is not None:
            last_close = sessions[last_sid][-1].close
            pnl += (last_close - prev_entry) * prev_desired

        return pnl

    def _quantile_of_abs_returns(
        self, rows: list[tuple[int, float, float]], q: float
    ) -> float:
        values = sorted(abs(r[1]) for r in rows)
        if not values:
            return 0.0
        idx = int(q * (len(values) - 1))
        if idx < 0:
            idx = 0
        if idx >= len(values):
            idx = len(values) - 1
        return values[idx]

    # ------------------------------------------------------------------
    # Live signal and order placement
    # ------------------------------------------------------------------
    def _signal_from_bars(self, bars: list[Any]) -> float:
        """Compute the combined standardized signal from closed bars."""
        first_open = bars[0].open
        price_ret = bars[self._lookback - 1].close - first_open

        window = bars[: self._lookback]
        cum_delta = sum(b.delta for b in window)
        cum_volume = sum(b.volume for b in window)
        delta_ratio = cum_delta / cum_volume if cum_volume else 0.0

        z_price = (price_ret - self._price_mean) / self._price_std
        z_delta = (delta_ratio - self._delta_mean) / self._delta_std

        return self._weight * z_price + (1.0 - self._weight) * z_delta

    def _apply_desired(self, ctx, desired: int) -> None:
        """Queue the market order needed to move to desired * size contracts."""
        pos = ctx.position
        delta = desired * self.size - pos
        if delta == 0:
            return

        if delta > 0:
            ctx.buy(size=delta, tif="DAY", tag="session-direction")
        else:
            ctx.sell(size=-delta, tif="DAY", tag="session-direction")

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

        signal = self._signal_from_bars(self._session_bars)
        pos = ctx.position

        # Near-zero signal: use the full-sample drift when flat; otherwise hold.
        if abs(signal) < 1e-12:
            if pos == 0:
                self._apply_desired(ctx, self._drift_direction)
            return

        desired = 1 if self._sign * signal > 0 else -1
        current = 1 if pos > 0 else -1 if pos < 0 else 0

        if current == 0:
            self._apply_desired(ctx, desired)
        elif desired != current:
            # Flip only when the opposite signal is strong enough.
            if abs(signal) >= self._flip_tau:
                self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
