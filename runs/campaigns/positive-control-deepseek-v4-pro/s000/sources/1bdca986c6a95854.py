"""A session-level directional strategy fitted once in setup().

This is the next iteration of the best prior shape (trial #34): one position
held nearly continuously, with a direction chosen early in each session and a
fitted flip threshold that suppresses whipsaw.

Changes from #34:

- The training backtest used for fitting models entry at the next bar's open
  rather than at the signal bar's close, matching the engine's T+1 fill rule
  more closely.
- The lookback, sign, and flip threshold are chosen jointly. For each candidate
  lookback/sign, we evaluate a small set of flip-threshold quantiles and pick
  the threshold that keeps at least `keep_fraction` of the best gross P&L while
  minimising the number of market fills.
- A small turnover penalty acts as a tie-breaker during fitting so that, among
  essentially equivalent P&L choices, fewer fills are preferred.
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Fitted early-session direction, held with minimal turnover."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 1,
        max_lookback: int = 8,
        min_samples: int = 15,
        keep_fraction: float = 0.85,
        quantile_candidates: tuple[float, ...] = (0.25, 0.5, 0.75),
    ) -> None:
        self.interval = interval
        self.size = size
        self.max_lookback = max_lookback
        self.min_samples = min_samples
        self.keep_fraction = keep_fraction
        self.quantile_candidates = quantile_candidates

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

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        self._fit(ctx)

    # ------------------------------------------------------------------
    # Fitting
    # ------------------------------------------------------------------
    def _fit(self, ctx) -> None:
        """Fit lookback, direction, and flip threshold from training bars."""
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

        best_found = False
        best_score: float | None = None

        for k in range(1, self.max_lookback + 1):
            rows: list[tuple[int, float, float]] = []
            for sid in sorted_sessions:
                sb = sessions[sid]
                if len(sb) < k + 1:
                    continue
                early_ret = sb[k - 1].close - sb[0].open
                if abs(early_ret) < 1e-12:
                    continue
                # Model the fill at the next bar's open (T+1).
                next_open = sb[k].open
                rows.append((sid, early_ret, next_open))

            if len(rows) < self.min_samples:
                continue

            for sign in (1, -1):
                candidates: list[tuple[float, float, float, int]] = []
                for q in self.quantile_candidates:
                    tau = self._tau_for_quantile(rows, q)
                    gross, trades = self._eval_rows(rows, last_close, sign, tau)
                    candidates.append((q, tau, gross, trades))

                best_gross = max(c[2] for c in candidates)
                floor = self.keep_fraction * best_gross
                eligible = [c for c in candidates if c[2] >= floor]
                if not eligible:
                    eligible = candidates

                # Prefer fewer fills; tie-break toward higher quantile
                # (higher threshold, fewer flips).
                eligible.sort(key=lambda c: (c[3], -c[0]))
                chosen_q, chosen_tau, chosen_gross, chosen_trades = eligible[0]

                # Tiny turnover penalty only as a deterministic tie-breaker.
                comparison_score = chosen_gross - chosen_trades * 1e-12
                if best_score is None or comparison_score > best_score:
                    best_score = comparison_score
                    best_found = True
                    self._lookback = k
                    self._sign = sign
                    self._flip_tau = chosen_tau

        if not best_found:
            self._fallback()

    def _fallback(self) -> None:
        self._lookback = 1
        self._sign = 1
        self._flip_tau = 0.0

    def _tau_for_quantile(
        self, rows: list[tuple[int, float, float]], q: float
    ) -> float:
        if q <= 0.0:
            return 0.0
        if q >= 1.0:
            return float("inf")

        values = sorted(abs(r[1]) for r in rows)
        if not values:
            return 0.0
        idx = int(q * len(values))
        if idx >= len(values):
            idx = len(values) - 1
        return values[idx]

    def _eval_rows(
        self,
        rows: list[tuple[int, float, float]],
        last_close: float,
        sign: int,
        tau: float,
    ) -> tuple[float, int]:
        """Continuous-holding gross P&L and number of market fills.

        This version models the fill at the next bar's open, matching the
        engine's T+1 eligibility rule for an order placed on the signal bar.
        """
        pnl = 0.0
        trades = 0
        holding = False
        current_desired = 1
        current_entry = 0.0

        for _sid, early_ret, next_open in rows:
            desired = 1 if sign * early_ret > 0 else -1

            if not holding:
                holding = True
                current_desired = desired
                current_entry = next_open
                trades += 1
                continue

            if desired != current_desired and abs(early_ret) >= tau:
                pnl += (next_open - current_entry) * current_desired
                current_desired = desired
                current_entry = next_open
                trades += 1

        if holding:
            pnl += (last_close - current_entry) * current_desired

        return pnl, trades

    # ------------------------------------------------------------------
    # Runtime helpers
    # ------------------------------------------------------------------
    def _direction_for_early_ret(self, early_ret: float) -> int:
        if early_ret > 0:
            return self._sign
        return -self._sign

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
        early_ret = bar.close - first_open
        pos = ctx.position

        # Flat early return: establish exposure on the full-sample drift if
        # currently flat, otherwise do nothing.
        if abs(early_ret) < 1e-12:
            if pos == 0:
                self._apply_desired(ctx, self._drift_direction)
            return

        desired = self._direction_for_early_ret(early_ret)
        current = 1 if pos > 0 else -1 if pos < 0 else 0

        if current == 0:
            self._apply_desired(ctx, desired)
        elif desired != current and abs(early_ret) >= self._flip_tau:
            self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
