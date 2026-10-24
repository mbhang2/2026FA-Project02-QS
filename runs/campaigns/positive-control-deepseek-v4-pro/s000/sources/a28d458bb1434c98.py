"""Asymmetric session-direction strategy, fitted in setup.

The prior best (#61) uses an early-session return to set direction and a
symmetric flip threshold.  This iteration keeps that low-turnover, one-decision-
per-session shape, but fits two flip thresholds instead of one: a lower
threshold when the signal would flip the position *toward* the full-sample
drift direction, and a higher threshold when it would flip *against* that drift.
The idea is to preserve the persistent-drifting-position behaviour that made
#61 profitable while avoiding marginal counter-trend flips.

The parameters -- interval, lookback, sign, and the two drift-aware
thresholds -- are chosen in setup() from the training bars by simulating the
live path gross P&L, so the fit matches the actual trader logic rather than a
simplified mark-to-market approximation.
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Fitted session-direction with drift-aware flip thresholds."""

    def __init__(
        self,
        *,
        candidate_intervals: tuple[str, ...] = ("1m", "5m", "15m", "30m"),
        size: int = 3,
        max_lookback: int = 12,
        toward_quantiles: tuple[float, ...] = (0.10, 0.20, 0.30),
        against_quantiles: tuple[float, ...] = (0.50, 0.70, 0.90),
        min_samples: int = 10,
    ) -> None:
        self.candidate_intervals = candidate_intervals
        self.size = size
        self.max_lookback = max_lookback
        self.toward_quantiles = toward_quantiles
        self.against_quantiles = against_quantiles
        self.min_samples = min_samples

        # Fitted in setup()
        self.interval = "5m"
        self._lookback = 1
        self._sign = 1
        self._drift_direction = 1
        self._flip_tau_towards = 0.0
        self._flip_tau_against = 0.0

        # Session state
        self._session: int | None = None
        self._bars_this_session = 0
        self._session_bars: list[Any] = []
        self._evaluated_this_session = False

    def setup(self, ctx) -> None:
        """Choose interval and parameters from training bars, then subscribe."""
        best = None

        for interval in self.candidate_intervals:
            try:
                bars = tuple(ctx.train_bars(interval))
            except Exception:
                continue
            if not bars:
                continue

            sessions: dict[int, list[Any]] = {}
            for bar in bars:
                sessions.setdefault(bar.session, []).append(bar)
            ordered = sorted(sessions)

            if len(ordered) < self.min_samples:
                continue

            result = self._fit_interval(sessions, ordered, bars)
            if result is None:
                continue

            score, lookback, sign, drift, tau_tow, tau_ag = result
            if best is None or score > best[0]:
                best = (score, interval, lookback, sign, drift, tau_tow, tau_ag)

        if best is not None:
            (
                _,
                self.interval,
                self._lookback,
                self._sign,
                self._drift_direction,
                self._flip_tau_towards,
                self._flip_tau_against,
            ) = best

        ctx.subscribe(self.interval)

    def _fit_interval(
        self,
        sessions: dict[int, list[Any]],
        ordered: list[int],
        bars: tuple[Any, ...],
    ) -> tuple[float, int, int, int, float, float] | None:
        """Fit all parameters for one candidate bar interval."""
        drift = 1 if bars[-1].close >= bars[0].close else -1

        best_score: float | None = None
        best_params = None

        for k in range(1, self.max_lookback + 1):
            values = []
            for sid in ordered:
                sb = sessions[sid]
                if len(sb) < k:
                    continue
                early_ret = sb[k - 1].close - sb[0].open
                values.append(abs(early_ret))

            if len(values) < self.min_samples:
                continue
            values.sort()

            for q_tow in self.toward_quantiles:
                tau_tow = self._quantile(values, q_tow)
                for q_ag in self.against_quantiles:
                    tau_ag = self._quantile(values, q_ag)
                    for sign in (1, -1):
                        score = self._simulate_path(
                            sessions,
                            ordered,
                            k,
                            sign,
                            drift,
                            tau_tow,
                            tau_ag,
                            self.size,
                        )
                        if best_score is None or score > best_score:
                            best_score = score
                            best_params = (k, sign, drift, tau_tow, tau_ag)

        if best_score is None or best_params is None:
            return None

        return (
            best_score,
            best_params[0],
            best_params[1],
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
        k: int,
        sign: int,
        drift: int,
        tau_towards: float,
        tau_against: float,
        size: int,
    ) -> float:
        """Simulate the actual live trading rule over training sessions.

        Returns gross P&L, not net of commission.  This mirrors the live path:
        one look per session at bar k, hold across sessions, and flip only when
        an opposite signal clears the relevant drift-aware threshold.
        """
        pos_contracts = 0
        prev_entry = None
        pnl = 0.0

        for sid in ordered:
            sb = sessions[sid]
            if len(sb) < k:
                continue

            first_open = sb[0].open
            entry_close = sb[k - 1].close
            early_ret = entry_close - first_open

            if abs(early_ret) < 1e-12:
                desired = drift
            else:
                desired = sign if early_ret > 0 else -sign

            current = 1 if pos_contracts > 0 else -1 if pos_contracts < 0 else 0

            if current == 0:
                pos_contracts = desired * size
                prev_entry = entry_close
            elif desired != current:
                tau = tau_towards if desired == drift else tau_against
                if abs(early_ret) >= tau:
                    pnl += (entry_close - prev_entry) * pos_contracts
                    pos_contracts = desired * size
                    prev_entry = entry_close

        if prev_entry is not None and pos_contracts != 0 and ordered:
            last_session = ordered[-1]
            if last_session in sessions and sessions[last_session]:
                last_close = sessions[last_session][-1].close
                pnl += (last_close - prev_entry) * pos_contracts

        return pnl

    def _desired_for_early_ret(self, early_ret: float) -> int:
        if abs(early_ret) < 1e-12:
            return self._drift_direction
        return self._sign if early_ret > 0 else -self._sign

    def _apply_desired(self, ctx, desired: int) -> None:
        """Queue the market order needed to move from current position to desired."""
        pos = ctx.position

        if desired > 0:
            if pos == 0:
                ctx.buy(size=self.size, tif="DAY", tag="session-long")
            elif pos < 0:
                ctx.buy(size=-pos + self.size, tif="DAY", tag="session-flip-long")
        elif desired < 0:
            if pos == 0:
                ctx.sell(size=self.size, tif="DAY", tag="session-short")
            elif pos > 0:
                ctx.sell(size=pos + self.size, tif="DAY", tag="session-flip-short")

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return

        bar = ctx.bars[self.interval]

        # Reset per-session state on session changes.
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
        elif desired != current:
            tau = (
                self._flip_tau_towards
                if desired == self._drift_direction
                else self._flip_tau_against
            )
            if abs(early_ret) >= tau:
                self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
