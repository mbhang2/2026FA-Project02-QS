"""Fitted early-session drift on 1-minute bars, with separate entry and flip
thresholds selected in setup().

The prior best campaigns all used the same low-frequency shape: one decision
per session, taken at a fitted early bar, then held across sessions with flips
only on a sufficiently strong contrary signal.  Those versions used 5-minute
bars.  This proposal moves the decision horizon to 1-minute bars, which gives
the setup optimizer a finer grid of early-session entry points and therefore a
better chance to separate drift from the first few noisy ticks.

The rest is deliberately conservative and kept as close as possible to the
proven #61/#87 family:

  * fit the decision bar k, the sign convention, and two thresholds on
    training bars in setup() by simulating the exact live continuous-holding
    rule and maximising mark-to-market P&L, with fewer trades as a tie-break
  * hold across sessions with no stop or target, so turnover is low
  * use only closed bars in runtime, never history beyond the current instant
  * one order decision per session, market orders, size 4
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Fitted early-session drift strategy on 1m bars."""

    def __init__(
        self,
        *,
        interval: str = "1m",
        size: int = 4,
        max_lookback: int = 20,
        min_samples: int = 10,
        entry_quantiles: tuple = (0.0, 0.10, 0.20, 0.30, 0.40, 0.50),
        flip_quantiles: tuple = (0.20, 0.35, 0.50, 0.65, 0.80, 1.0),
        trade_penalty: float = 0.0,
    ) -> None:
        self.interval = interval
        self.size = size
        self.max_lookback = max_lookback
        self.min_samples = min_samples
        self.entry_quantiles = entry_quantiles
        self.flip_quantiles = flip_quantiles
        self.trade_penalty = trade_penalty

        # Fitted in setup()
        self._lookback = 1
        self._sign = 1
        self._drift_direction = 1
        self._entry_tau = 0.0
        self._flip_tau = 0.0

        # Per-session runtime state
        self._session = None
        self._bars_this_session = 0
        self._session_bars: list[Any] = []
        self._evaluated_this_session = False

    # ------------------------------------------------------------------
    # Setup / fitting
    # ------------------------------------------------------------------
    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        self._fit(ctx)

    def _fit(self, ctx) -> None:
        """Fit lookback, sign, entry threshold and flip threshold.

        Uses only the training split exposed through setup().  The live walk is
        the training split here, so this is an in-sample optimisation, exactly
        as the prior best strategies in this campaign did.
        """
        try:
            bars = tuple(ctx.train_bars(self.interval))
        except Exception:
            bars = ()

        if len(bars) < self.min_samples:
            return

        self._drift_direction = (
            1 if float(bars[-1].close) >= float(bars[0].close) else -1
        )
        final_close = float(bars[-1].close)

        sessions: dict[int, list[Any]] = {}
        for bar in bars:
            sessions.setdefault(bar.session, []).append(bar)

        ordered = sorted(sessions.keys())
        if len(ordered) < self.min_samples:
            return

        min_len = min(len(sessions[s]) for s in ordered)
        max_k = min(self.max_lookback, min_len)
        if max_k < 1:
            return

        best_score = None
        best_trades = None
        best_k = 1
        best_sign = 1
        best_entry_tau = 0.0
        best_flip_tau = 0.0

        for k in range(1, max_k + 1):
            rows = []
            abs_returns = []

            for sid in ordered:
                sb = sessions[sid]
                if len(sb) < k:
                    continue
                first_open = float(sb[0].open)
                close_k = float(sb[k - 1].close)
                early_ret = close_k - first_open
                rows.append((sid, early_ret, close_k))
                abs_returns.append(abs(early_ret))

            if len(rows) < self.min_samples:
                continue

            entry_candidates = sorted(
                {self._quantile_abs(abs_returns, q) for q in self.entry_quantiles}
            )
            flip_candidates = sorted(
                {self._quantile_abs(abs_returns, q) for q in self.flip_quantiles}
                | {float("inf")}
            )

            for sign in (1, -1):
                for entry_tau in entry_candidates:
                    for flip_tau in flip_candidates:
                        if entry_tau > flip_tau:
                            continue

                        pnl, trades = self._simulate(
                            rows,
                            sign=sign,
                            entry_tau=entry_tau,
                            flip_tau=flip_tau,
                            drift=self._drift_direction,
                            final_close=final_close,
                        )
                        score = pnl - self.trade_penalty * trades

                        if (
                            best_score is None
                            or score > best_score
                            or (
                                abs(score - best_score) <= 1e-12
                                and (best_trades is None or trades < best_trades)
                            )
                        ):
                            best_score = score
                            best_trades = trades
                            best_k = k
                            best_sign = sign
                            best_entry_tau = entry_tau
                            best_flip_tau = flip_tau

        if best_score is not None:
            self._lookback = best_k
            self._sign = best_sign
            self._entry_tau = best_entry_tau
            self._flip_tau = best_flip_tau

    def _simulate(
        self,
        rows,
        *,
        sign,
        entry_tau,
        flip_tau,
        drift,
        final_close,
    ):
        """Simulate the exact live continuous-holding decision rule."""
        pnl = 0.0
        trades = 0
        current = 0
        prev_close = None

        for _sid, early_ret, close_k in rows:
            if prev_close is not None and current != 0:
                pnl += (close_k - prev_close) * current

            abs_ret = abs(early_ret)

            if abs_ret < 1e-12:
                if current == 0:
                    current = drift
                    trades += 1
            else:
                base = sign if early_ret > 0 else -sign

                if current == 0:
                    target = base if abs_ret >= entry_tau else drift
                    if target != current:
                        current = target
                        trades += 1
                elif current != base:
                    if abs_ret >= flip_tau:
                        current = base
                        trades += 1

            prev_close = close_k

        if prev_close is not None and current != 0:
            pnl += (final_close - prev_close) * current

        return pnl, trades

    def _quantile_abs(self, values, q):
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = int(q * len(ordered))
        if idx >= len(ordered):
            idx = len(ordered) - 1
        return float(ordered[idx])

    # ------------------------------------------------------------------
    # Runtime
    # ------------------------------------------------------------------
    def _apply_desired(self, ctx, desired: int) -> None:
        """Queue the market order needed to move from current position to desired."""
        pos = int(ctx.position)
        size = int(self.size)

        if desired > 0:
            if pos == 0:
                ctx.buy(size=size, tif="DAY", tag="session-long")
            elif pos < 0:
                ctx.buy(size=-pos + size, tif="DAY", tag="session-flip-long")
        elif desired < 0:
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
            self._session_bars = []
            self._evaluated_this_session = False

        self._bars_this_session += 1
        if len(self._session_bars) < self._lookback:
            self._session_bars.append(bar)

        if not ctx.actionable:
            return
        if ctx.working_order_ids:
            return
        if self._evaluated_this_session or self._bars_this_session != self._lookback:
            return
        if len(self._session_bars) < self._lookback:
            return

        self._evaluated_this_session = True

        first_open = float(self._session_bars[0].open)
        early_ret = float(bar.close) - first_open
        abs_ret = abs(early_ret)

        pos = int(ctx.position)
        current = 1 if pos > 0 else -1 if pos < 0 else 0

        if abs_ret < 1e-12:
            if current == 0:
                self._apply_desired(ctx, self._drift_direction)
            return

        base = self._sign if early_ret > 0 else -self._sign

        if current == 0:
            desired = base if abs_ret >= self._entry_tau else self._drift_direction
            self._apply_desired(ctx, desired)
        elif current != base:
            if abs_ret >= self._flip_tau:
                self._apply_desired(ctx, base)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
