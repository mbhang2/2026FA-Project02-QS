"""Candidate: fit the decision interval, lookback and thresholds directly.

The best prior trials (#60/#61/#62/#95/#96) all share one shape: one early-
session decision per session, a fitted sign and lookback, and a continuous
position held across sessions with flips only on a sufficiently strong contrary
early return.  This version keeps that exact runtime shape and the winning
size=3, but widens the fit to choose between several bar intervals (30s, 1m,
2m, 5m) by simulating the actual decision rule on the training bars, including
entry and flip thresholds and a small per-contract turnover penalty.

The earlier source chose 5m manually.  On a 390-tick simulated session, 5m
only yields one or two bars, so the lookback search had almost no room to move.
A finer interval offers more possible decision bars while still firing only one
tradable decision per session, which is the same low-turnover structure that
produced the best scores so far.
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Fitted early-session drift with interval selection."""

    def __init__(
        self,
        *,
        candidate_intervals: tuple = ("30s", "1m", "2m", "5m"),
        size: int = 3,
        max_lookback: int = 40,
        min_samples: int = 15,
        entry_quantiles: tuple = (0.0, 0.10, 0.20, 0.30, 0.40, 0.50),
        flip_quantiles: tuple = (0.20, 0.25, 0.30, 0.35, 0.40, 0.50),
        trade_penalty_per_contract: float = 0.0002,
        exposure_floor: float = 0.70,
    ) -> None:
        self.candidate_intervals = candidate_intervals
        self.size = size
        self.max_lookback = max_lookback
        self.min_samples = min_samples
        self.entry_quantiles = entry_quantiles
        self.flip_quantiles = flip_quantiles
        self.trade_penalty_per_contract = trade_penalty_per_contract
        self.exposure_floor = exposure_floor

        # Reasonable fallbacks; overwritten in setup().
        self.interval = "5m"
        self._lookback = 1
        self._sign = 1
        self._drift_direction = 1
        self._entry_tau = 0.0
        self._flip_tau = 0.0

        # Per-session runtime state.
        self._session: int | None = None
        self._bars_this_session = 0
        self._session_open_price: float | None = None
        self._evaluated_this_session = False

    # ------------------------------------------------------------------
    # Setup / fitting
    # ------------------------------------------------------------------
    def setup(self, ctx) -> None:
        self._fit(ctx)
        # Declare exactly one subscription, chosen by the fit.  Train bars can
        # be requested before a subscription is declared; this is the one place
        # bulk data is available.
        ctx.subscribe(self.interval)

    @staticmethod
    def _quantile_abs(values: list[float], q: float) -> float:
        """Return a quantile of absolute feature values."""
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = int(q * len(ordered))
        if idx >= len(ordered):
            idx = len(ordered) - 1
        return float(ordered[idx])

    def _fit(self, ctx) -> None:
        """Fit interval, lookback, sign, entry threshold and flip threshold."""
        best_key = None

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

            final_close = float(sessions[ordered[-1]][-1].close)
            drift = (
                1
                if float(sessions[ordered[-1]][-1].close)
                >= float(sessions[ordered[0]][0].open)
                else -1
            )

            max_bars = max(len(sessions[sid]) for sid in ordered)
            for k in range(1, min(self.max_lookback, max_bars) + 1):
                rows: list[tuple[int, float, float]] = []
                for sid in ordered:
                    sb = sessions[sid]
                    if len(sb) < k:
                        continue
                    early_ret = float(sb[k - 1].close) - float(sb[0].open)
                    rows.append((sid, early_ret, float(sb[k - 1].close)))

                if len(rows) < self.min_samples:
                    continue

                abs_rets = sorted(abs(r[1]) for r in rows)
                if not abs_rets:
                    continue

                for sign in (1, -1):
                    for qe in self.entry_quantiles:
                        entry_tau = (
                            0.0 if qe <= 0 else self._quantile_abs(abs_rets, qe)
                        )
                        for qf in self.flip_quantiles:
                            flip_tau = self._quantile_abs(abs_rets, qf)

                            pnl, contracts, exposed = self._simulate(
                                rows,
                                sign,
                                drift,
                                entry_tau,
                                flip_tau,
                                final_close,
                            )

                            score = pnl - self.trade_penalty_per_contract * contracts
                            if exposed < self.exposure_floor * len(rows):
                                score -= 10000.0

                            key = (
                                round(score, 10),
                                -int(contracts),
                                -int(exposed),
                            )
                            if best_key is None or key > best_key:
                                best_key = key
                                self.interval = interval
                                self._lookback = k
                                self._sign = sign
                                self._drift_direction = drift
                                self._entry_tau = entry_tau
                                self._flip_tau = flip_tau

    def _simulate(
        self,
        rows: list[tuple[int, float, float]],
        sign: int,
        drift: int,
        entry_tau: float,
        flip_tau: float,
        final_close: float,
    ) -> tuple[float, int, int]:
        """Simulate the continuous-holding runtime rule on rows."""
        current = 0
        prev_ref: float | None = None
        pnl = 0.0
        contracts = 0
        exposed = 0

        for _sid, early_ret, ref in rows:
            if prev_ref is not None and current != 0:
                pnl += (ref - prev_ref) * current * self.size

            desired = self._desired_state(
                current, early_ret, sign, drift, entry_tau, flip_tau
            )

            if desired != current:
                contracts += abs(desired - current) * self.size
                current = desired

            if current != 0:
                exposed += 1

            prev_ref = ref

        if prev_ref is not None and current != 0:
            pnl += (final_close - prev_ref) * current * self.size

        return pnl, int(contracts), int(exposed)

    @staticmethod
    def _desired_state(
        current: int,
        early_ret: float,
        sign: int,
        drift: int,
        entry_tau: float,
        flip_tau: float,
    ) -> int:
        """Return desired position +1, -1, or 0 for one session's signal."""
        eps = 1e-12

        if abs(early_ret) < eps:
            # Flat early return: keep an existing position, otherwise trade the
            # full-sample drift direction.
            return drift if current == 0 else current

        base = sign if early_ret > 0 else -sign

        if current == 0:
            if abs(early_ret) < entry_tau:
                return 0
            return base

        # Already positioned: only flip on a sufficiently strong contrary signal.
        if base != current and abs(early_ret) >= flip_tau:
            return base

        return current

    # ------------------------------------------------------------------
    # Runtime
    # ------------------------------------------------------------------
    def _apply_desired(self, ctx, desired: int) -> None:
        """Queue the market order needed to move from current position to desired."""
        pos = int(ctx.position)
        size = int(self.size)

        if desired == 0:
            if pos != 0:
                ctx.close(tif="DAY", tag="flat")
            return

        if desired > 0:
            if pos == 0:
                ctx.buy(size=size, tif="DAY", tag="session-long")
            elif pos < 0:
                ctx.buy(size=-pos + size, tif="DAY", tag="session-flip-long")
        else:
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
            self._session_open_price = None
            self._evaluated_this_session = False

        if self._session_open_price is None:
            self._session_open_price = float(bar.open)

        self._bars_this_session += 1

        if not ctx.actionable:
            return
        if ctx.working_order_ids:
            return
        if self._evaluated_this_session or self._bars_this_session < self._lookback:
            return

        self._evaluated_this_session = True

        first_open = self._session_open_price
        early_ret = float(bar.close) - first_open

        pos = int(ctx.position)
        current = 1 if pos > 0 else -1 if pos < 0 else 0

        desired = self._desired_state(
            current,
            early_ret,
            self._sign,
            self._drift_direction,
            self._entry_tau,
            self._flip_tau,
        )

        self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
