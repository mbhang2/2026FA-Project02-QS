"""Refinement of the fitted early-session drift strategy.

The best prior trials in this campaign share one shape: one early-session
decision per session, a fitted sign and lookback, and a continuous position
held across sessions with flips only on a strong contrary early return.

This version keeps that exact runtime shape but improves the in-sample fit in
two ways.  First, sessions with a flat early-session return are included in the
fit rather than silently dropped, so the fitted parameters reflect the real
runtime behaviour (entering the full-sample drift direction when flat).
Second, the flip threshold is chosen by simulating the actual thresholded rule
on the training rows and maximising P&L net of an estimated per-contract cost,
instead of being fixed at the 0.25 quantile.

The runtime remains one decision per session: after lookback bars, compute
early return from session open, enter the fitted drift direction on a flat
early return, otherwise enter the signal direction; if already positioned,
flip only when an opposite signal exceeds the fitted threshold.
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Fitted early-session direction, with an improved flip-threshold fit."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 3,
        max_lookback: int = 8,
        q_candidates: tuple = (0.0, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50),
        cost_per_contract: float = 0.0002,
        min_samples: int = 15,
    ) -> None:
        self.interval = interval
        self.size = size
        self.max_lookback = max_lookback
        self.q_candidates = q_candidates
        self.cost_per_contract = cost_per_contract
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

    @staticmethod
    def _quantile_abs(values, q: float) -> float:
        """Quantile of absolute feature values, clamped to available data."""
        ordered = sorted(values)
        if not ordered:
            return 0.0
        idx = int(q * len(ordered))
        if idx >= len(ordered):
            idx = len(ordered) - 1
        return float(ordered[idx])

    def _fit(self, ctx) -> None:
        """Fit lookback, sign, drift direction, and flip threshold."""
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

        ordered_sessions = sorted(sessions.keys())

        best_base = None
        best_k = 1
        best_sign = 1
        best_rows = None

        for k in range(1, self.max_lookback + 1):
            rows = []
            for sid in ordered_sessions:
                sb = sessions[sid]
                if len(sb) < k + 1:
                    continue
                early_ret = float(sb[k - 1].close) - float(sb[0].open)
                rows.append((sid, early_ret, float(sb[k - 1].close)))

            if len(rows) < self.min_samples:
                continue

            final_close = float(sessions[ordered_sessions[-1]][-1].close)
            for sign in (1, -1):
                gross = self._simulate_base(
                    rows, sign, self._drift_direction, final_close
                )
                if best_base is None or gross > best_base:
                    best_base = gross
                    best_k = k
                    best_sign = sign
                    best_rows = rows

        if best_rows is None:
            self._lookback = 1
            self._sign = 1
            self._flip_tau = 0.0
            return

        self._lookback = best_k
        self._sign = best_sign

        final_close = float(sessions[ordered_sessions[-1]][-1].close)

        best_net = None
        best_q = 0.25
        best_tau = 0.0

        for q in self.q_candidates:
            tau = self._quantile_abs([abs(r[1]) for r in best_rows], q)
            pnl, contracts = self._simulate_net(
                best_rows,
                self._sign,
                self._drift_direction,
                tau,
                final_close,
            )
            net = pnl - self.cost_per_contract * contracts
            if best_net is None or net > best_net:
                best_net = net
                best_q = q
                best_tau = tau

        self._flip_tau = best_tau

    def _simulate_base(self, rows, sign: int, drift: int, final_close: float) -> float:
        """Choose lookback/sign using gross continuous-holding P&L.

        Flat early-return sessions are included and handled as in runtime:
        enter drift if flat, otherwise hold current position.
        """
        current = 0
        prev_ref = None
        pnl = 0.0

        for _sid, early_ret, ref in rows:
            if prev_ref is not None and current != 0:
                pnl += (ref - prev_ref) * current * self.size

            if abs(early_ret) < 1e-12:
                desired = drift if current == 0 else current
            else:
                desired = sign if early_ret > 0 else -sign

            current = desired
            prev_ref = ref

        if prev_ref is not None and current != 0:
            pnl += (final_close - prev_ref) * current * self.size

        return pnl

    def _simulate_net(self, rows, sign: int, drift: int, tau: float, final_close: float):
        """Simulate the actual thresholded rule and return P&L and contracts."""
        current = 0
        prev_ref = None
        pnl = 0.0
        contracts = 0

        for _sid, early_ret, ref in rows:
            if prev_ref is not None and current != 0:
                pnl += (ref - prev_ref) * current * self.size

            if abs(early_ret) < 1e-12:
                desired = drift if current == 0 else current
            else:
                base = sign if early_ret > 0 else -sign
                if current == 0:
                    desired = base
                elif base != current and abs(early_ret) >= tau:
                    desired = base
                else:
                    desired = current

            if desired != current:
                contracts += abs(desired - current) * self.size
                current = desired

            prev_ref = ref

        if prev_ref is not None and current != 0:
            pnl += (final_close - prev_ref) * current * self.size

        return pnl, int(contracts)

    # ------------------------------------------------------------------
    # Runtime
    # ------------------------------------------------------------------
    def _desired_position(self, early_ret: float, current: int) -> int:
        """Return desired +/-1 position for one session's early return."""
        if abs(early_ret) < 1e-12:
            return self._drift_direction if current == 0 else current

        base = self._sign if early_ret > 0 else -self._sign

        if current == 0:
            return base
        if base != current and abs(early_ret) >= self._flip_tau:
            return base
        return current

    def _apply_desired(self, ctx, desired: int) -> None:
        """Queue the market order needed to move from current position to desired."""
        pos = int(ctx.position)

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

        first_open = float(self._session_bars[0].open)
        early_ret = float(bar.close) - first_open

        pos = int(ctx.position)
        current = 1 if pos > 0 else -1 if pos < 0 else 0
        desired = self._desired_position(early_ret, current)

        self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
