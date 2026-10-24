"""Next candidate in the fitted early-session drift family.

The best prior trials in this campaign (#60/#61/#92) share a low-turnover
continuous-holding shape: one 5m decision per session, fitted from training
bars, with positions held across sessions and flips only on a sufficiently
strong contrary early-session return.

This version keeps that exact runtime shape and the winning size=3 from #61.
It changes one piece of the fitting: #61 chose `k` and `sign` by maximizing a
pure mark-to-market P&L that did not model the flip guard, then imposed a fixed
0.25 quantile flip threshold.  Here, after choosing `k` and `sign` the same
way, the flip threshold is chosen from a conservative grid by simulating the
actual runtime rule (including the flip guard and a small per-contract turnover
penalty).  The practical effect is a slightly more careful flip threshold while
leaving the already-successful signal selection untouched.
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Fitted early-session drift, size=3, with a simulated flip-threshold pick."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 3,
        max_lookback: int = 8,
        min_samples: int = 15,
        flip_quantiles: tuple = (0.25, 0.30, 0.35, 0.40, 0.50),
        trade_penalty_per_contract: float = 0.0002,
        exposure_floor: float = 0.70,
    ) -> None:
        self.interval = interval
        self.size = size
        self.max_lookback = max_lookback
        self.min_samples = min_samples
        self.flip_quantiles = flip_quantiles
        self.trade_penalty_per_contract = trade_penalty_per_contract
        self.exposure_floor = exposure_floor

        # Fitted in setup()
        self._lookback = 1
        self._sign = 1
        self._drift_direction = 1
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
        """Return a quantile of absolute feature values."""
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = int(q * len(ordered))
        if idx >= len(ordered):
            idx = len(ordered) - 1
        return float(ordered[idx])

    def _fit(self, ctx) -> None:
        """Fit lookback, sign, drift direction, and flip threshold from training bars."""
        try:
            bars = tuple(ctx.train_bars(self.interval))
        except Exception:
            bars = ()

        if not bars:
            return

        sessions: dict[int, list[Any]] = {}
        for bar in bars:
            sessions.setdefault(bar.session, []).append(bar)

        ordered = sorted(sessions)
        if len(ordered) < self.min_samples:
            return

        self._drift_direction = (
            1 if float(bars[-1].close) >= float(bars[0].close) else -1
        )
        final_close = float(sessions[ordered[-1]][-1].close)

        # Stage 1: choose k and sign exactly as the best prior trial did,
        # using a continuous-holding, no-flip-guard mark-to-market P&L.
        best_score = None
        best_k = 1
        best_sign = 1
        best_rows: list[tuple[int, float, float]] | None = None

        for k in range(1, self.max_lookback + 1):
            rows: list[tuple[int, float, float]] = []
            for sid in ordered:
                sb = sessions[sid]
                if len(sb) < k:
                    continue
                early_ret = float(sb[k - 1].close) - float(sb[0].open)
                if abs(early_ret) < 1e-12:
                    continue
                rows.append((sid, early_ret, float(sb[k - 1].close)))

            if len(rows) < self.min_samples:
                continue

            for sign in (1, -1):
                score = self._eval_sign_pure(rows, final_close, sign)
                if best_score is None or score > best_score:
                    best_score = score
                    best_k = k
                    best_sign = sign
                    best_rows = rows

        if best_rows is None:
            return

        self._lookback = best_k
        self._sign = best_sign

        # Stage 2: choose the flip threshold from a conservative grid by
        # simulating the actual runtime rule, including the flip guard.
        self._flip_tau = self._choose_flip_tau(best_rows, final_close)

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

    def _choose_flip_tau(
        self,
        rows: list[tuple[int, float, float]],
        final_close: float,
    ) -> float:
        """Choose the best flip threshold from the candidate quantiles."""
        abs_returns = sorted(abs(r[1]) for r in rows)
        if not abs_returns:
            return 0.0

        best_tau = 0.0
        best_key = None

        for q in self.flip_quantiles:
            tau = self._quantile_abs(abs_returns, q)
            pnl, contracts, exposed = self._simulate(
                rows, self._sign, self._drift_direction, tau, final_close
            )

            score = pnl - self.trade_penalty_per_contract * contracts
            if exposed < self.exposure_floor * len(rows):
                score -= 1000.0

            key = (round(score, 10), -int(contracts))
            if best_key is None or key > best_key:
                best_key = key
                best_tau = tau

        return best_tau

    def _simulate(
        self,
        rows: list[tuple[int, float, float]],
        sign: int,
        drift: int,
        tau: float,
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

            desired = self._desired_state(current, early_ret, sign, drift, tau)

            if desired != current:
                contracts += abs(desired - current) * self.size
                current = desired

            if current != 0:
                exposed += 1

            prev_ref = ref

        if prev_ref is not None and current != 0:
            pnl += (final_close - prev_ref) * current * self.size

        return pnl, contracts, exposed

    def _desired_state(
        self,
        current: int,
        early_ret: float,
        sign: int,
        drift: int,
        tau: float,
    ) -> int:
        """Return desired position +1, -1, or 0 for the current state."""
        eps = 1e-12

        if abs(early_ret) < eps:
            # Flat early return: keep existing position, or establish drift if flat.
            return drift if current == 0 else current

        base = sign if early_ret > 0 else -sign

        if current == 0:
            return base

        # Only flip on a strong contrary signal; small signals are likely noise
        # and turnover is not free.
        if base != current and abs(early_ret) >= tau:
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
            current, early_ret, self._sign, self._drift_direction, self._flip_tau
        )

        self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
