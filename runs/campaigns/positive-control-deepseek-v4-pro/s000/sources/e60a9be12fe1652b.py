"""Next candidate in the fitted early-session drift family.

The best prior trials (#60/#61/#95) share a low-turnover continuous-holding
shape: one 5-minute decision per session, fitted from training bars, holding
across sessions and flipping only on a sufficiently strong contrary early
return.  This version keeps that runtime shape and the successful fixed size of
3 contracts.

The one new freedom is an explicit *entry* threshold in addition to the flip
threshold.  Previous versions always established the initial position on the
first nonzero early return.  Here the fitted decision rule may stay flat until
an early return is large enough to pass a fitted entry threshold, which can
skip a small number of weak initial sessions while still keeping exposure well
above the 70% floor.  The entry threshold and the flip threshold are chosen
from quantile candidates by simulating the actual runtime rule on the training
bars, including a small per-contract turnover penalty.
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Fitted early-session drift with an entry threshold."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 3,
        max_lookback: int = 8,
        min_samples: int = 15,
        entry_quantiles: tuple = (0.0, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90),
        flip_quantiles: tuple = (0.20, 0.25, 0.30, 0.35, 0.40, 0.50),
        trade_penalty_per_contract: float = 0.0002,
        exposure_floor: float = 0.70,
    ) -> None:
        self.interval = interval
        self.size = size
        self.max_lookback = max_lookback
        self.min_samples = min_samples
        self.entry_quantiles = entry_quantiles
        self.flip_quantiles = flip_quantiles
        self.trade_penalty_per_contract = trade_penalty_per_contract
        self.exposure_floor = exposure_floor

        # Fitted in setup()
        self._lookback = 1
        self._sign = 1
        self._drift_direction = 1
        self._entry_tau = 0.0
        self._flip_tau = 0.0

        # Per-session runtime state
        self._session: int | None = None
        self._bars_this_session = 0
        self._session_open_price: float | None = None
        self._evaluated_this_session = False

    # ------------------------------------------------------------------
    # Setup / fitting
    # ------------------------------------------------------------------
    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        self._fit(ctx)

    def _quantile_abs(self, values: list[float], q: float) -> float:
        """A quantile of absolute feature values."""
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = int(q * len(ordered))
        if idx >= len(ordered):
            idx = len(ordered) - 1
        return float(ordered[idx])

    def _fit(self, ctx) -> None:
        """Fit lookback, sign, drift direction, entry threshold and flip threshold."""
        try:
            bars = tuple(ctx.train_bars(self.interval))
        except Exception:
            bars = ()

        if bars:
            self._drift_direction = (
                1 if float(bars[-1].close) >= float(bars[0].close) else -1
            )
        else:
            self._drift_direction = 1

        sessions: dict[int, list[Any]] = {}
        for bar in bars:
            sessions.setdefault(bar.session, []).append(bar)
        ordered = sorted(sessions)
        if len(ordered) < self.min_samples:
            return

        final_close = float(sessions[ordered[-1]][-1].close)

        # Stage 1: choose lookback and sign exactly as the best prior trials
        # did, using a continuous-holding no-threshold mark-to-market P&L.
        best_score = None
        best_k = 1
        best_sign = 1
        best_sim_rows: list[tuple[int, float, float]] | None = None

        for k in range(1, self.max_lookback + 1):
            rows: list[tuple[int, float, float]] = []
            all_rows: list[tuple[int, float, float]] = []

            for sid in ordered:
                sb = sessions[sid]
                if len(sb) < k:
                    continue
                early_ret = float(sb[k - 1].close) - float(sb[0].open)
                ref = float(sb[k - 1].close)
                all_rows.append((sid, early_ret, ref))
                if abs(early_ret) >= 1e-12:
                    rows.append((sid, early_ret, ref))

            if len(rows) < self.min_samples:
                continue

            for sign in (1, -1):
                score = self._eval_sign_pure(rows, final_close, sign)
                if best_score is None or score > best_score:
                    best_score = score
                    best_k = k
                    best_sign = sign
                    best_sim_rows = all_rows

        if best_sim_rows is None:
            return

        self._lookback = best_k
        self._sign = best_sign

        # Stage 2: choose entry and flip thresholds by simulating the actual
        # runtime decision rule, including the entry threshold.
        abs_returns = sorted(abs(r[1]) for r in best_sim_rows)
        if not abs_returns:
            return

        best_key = None
        best_entry_tau = 0.0
        best_flip_tau = 0.0

        for qe in self.entry_quantiles:
            entry_tau = 0.0 if qe <= 0 else self._quantile_abs(abs_returns, qe)
            for qf in self.flip_quantiles:
                flip_tau = self._quantile_abs(abs_returns, qf)

                pnl, contracts, exposed = self._simulate(
                    best_sim_rows,
                    self._sign,
                    self._drift_direction,
                    entry_tau,
                    flip_tau,
                    final_close,
                )

                score = pnl - self.trade_penalty_per_contract * contracts
                if exposed < self.exposure_floor * len(best_sim_rows):
                    score -= 1000.0

                key = (round(score, 10), -int(contracts))
                if best_key is None or key > best_key:
                    best_key = key
                    best_entry_tau = entry_tau
                    best_flip_tau = flip_tau

        self._entry_tau = best_entry_tau
        self._flip_tau = best_flip_tau

    def _eval_sign_pure(
        self,
        rows: list[tuple[int, float, float]],
        final_close: float,
        sign: int,
    ) -> float:
        """Continuous-holding mark-to-market P&L for a fixed sign convention."""
        pnl = 0.0
        prev_desired = None
        prev_entry = None

        for _sid, early_ret, entry_close in rows:
            desired = 1 if sign * early_ret > 0 else -1

            if prev_desired is not None and prev_entry is not None:
                pnl += (entry_close - prev_entry) * prev_desired

            prev_desired = desired
            prev_entry = entry_close

        if prev_desired is not None and prev_entry is not None:
            pnl += (final_close - prev_entry) * prev_desired

        return pnl

    def _simulate(
        self,
        rows: list[tuple[int, float, float]],
        sign: int,
        drift: int,
        entry_tau: float,
        flip_tau: float,
        final_close: float,
    ) -> tuple[float, int, int]:
        """Simulate the actual continuous-holding runtime rule."""
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

    def _desired_state(
        self,
        current: int,
        early_ret: float,
        sign: int,
        drift: int,
        entry_tau: float,
        flip_tau: float,
    ) -> int:
        """Return desired position: +1, -1, or 0 (flat)."""
        eps = 1e-12

        if abs(early_ret) < eps:
            base = drift
        else:
            base = sign if early_ret > 0 else -sign

        if current == 0:
            if abs(early_ret) < entry_tau:
                return 0
            return base

        # Already positioned: ignore a zero early return, and only flip on a
        # sufficiently strong contrary signal.
        if abs(early_ret) < eps:
            return current

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
