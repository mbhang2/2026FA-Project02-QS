"""Fitted session-direction strategy with interval selection.

Prior best (#61) used 5m bars and one early-session return.  This iteration
searches across bar intervals, lookbacks, signs, and flip thresholds on the
training bars in `setup`, and keeps the combination with the best in-sample
continuous-holding P&L.  Live trading then uses that combination.

Position size remains 3 because trial #62 showed size=4 did not improve net
P&L.
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Fitted early-session direction, with bar interval chosen from data."""

    def __init__(
        self,
        *,
        candidate_intervals: tuple[str, ...] = ("1m", "5m", "15m", "30m"),
        max_lookback: int = 12,
        size: int = 3,
        flip_quantiles: tuple[float, ...] = (0.20, 0.25, 0.35, 0.50),
    ) -> None:
        self.candidate_intervals = candidate_intervals
        self.max_lookback = max_lookback
        self.size = size
        self.flip_quantiles = flip_quantiles

        # Fitted in setup()
        self.interval = "5m"
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
        best = None

        for interval in self.candidate_intervals:
            try:
                bars = tuple(ctx.train_bars(interval))
            except Exception:
                continue
            if not bars:
                continue

            result = self._fit_interval(bars)
            if result is None:
                continue

            score, lookback, sign, flip_tau, drift = result
            if best is None or score > best[0]:
                best = (score, interval, lookback, sign, flip_tau, drift)

        if best is not None:
            _, self.interval, self._lookback, self._sign, self._flip_tau, self._drift_direction = best

        ctx.subscribe(self.interval)

    def _fit_interval(self, bars: tuple[Any, ...]):
        sessions: dict[int, list[Any]] = {}
        for bar in bars:
            sessions.setdefault(bar.session, []).append(bar)
        ordered = sorted(sessions)

        drift = 1 if bars[-1].close >= bars[0].close else -1

        best_score = None
        best_params = None

        for k in range(1, self.max_lookback + 1):
            vals = []
            for sid in ordered:
                sb = sessions[sid]
                if len(sb) < k:
                    continue
                early_ret = sb[k - 1].close - sb[0].open
                if abs(early_ret) > 1e-12:
                    vals.append(abs(early_ret))

            if not vals:
                continue
            vals.sort()

            for q in self.flip_quantiles:
                idx = min(int(q * (len(vals) - 1)), len(vals) - 1)
                flip_tau = vals[idx]

                for sign in (1, -1):
                    score = self._score_path(
                        sessions, ordered, k, sign, flip_tau, drift
                    )
                    if best_score is None or score > best_score:
                        best_score = score
                        best_params = (k, sign, flip_tau)

        if best_params is None:
            return None

        return best_score, best_params[0], best_params[1], best_params[2], drift

    def _score_path(
        self,
        sessions: dict[int, list[Any]],
        ordered: list[int],
        k: int,
        sign: int,
        flip_tau: float,
        drift: int,
    ) -> float:
        pos = 0
        prev_entry = None
        pnl = 0.0

        for sid in ordered:
            sb = sessions[sid]
            if len(sb) < k:
                continue

            first_open = sb[0].open
            entry_close = sb[k - 1].close
            early_ret = entry_close - first_open

            if pos == 0:
                desired = self._desired_for_early_value(early_ret, sign, drift)
                pos = desired * self.size
                prev_entry = entry_close
                continue

            if abs(early_ret) < 1e-12:
                continue

            desired = sign if early_ret > 0 else -sign
            current = 1 if pos > 0 else -1

            if desired != current and abs(early_ret) >= flip_tau:
                pnl += (entry_close - prev_entry) * pos
                pos = desired * self.size
                prev_entry = entry_close

        if prev_entry is not None and pos != 0:
            last_close = sessions[ordered[-1]][-1].close
            pnl += (last_close - prev_entry) * pos

        return pnl

    def _desired_for_early_value(self, early_ret: float, sign: int, drift: int) -> int:
        if abs(early_ret) < 1e-12:
            return drift
        return sign if early_ret > 0 else -sign

    def _desired_for_early_ret(self, early_ret: float) -> int:
        if abs(early_ret) < 1e-12:
            return self._drift_direction
        return self._sign if early_ret > 0 else -self._sign

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
        else:
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
            if pos == 0:
                self._apply_desired(ctx, self._drift_direction)
            return

        desired = self._desired_for_early_ret(early_ret)
        current = 1 if pos > 0 else -1 if pos < 0 else 0

        if current == 0:
            self._apply_desired(ctx, desired)
        elif desired != current and abs(early_ret) >= self._flip_tau:
            self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
