"""Refinement of the best prior strategy family.

The strongest prior trials (#60, #61, #68, #91, #92) all use an early-session
drift signal on five-minute bars: one decision per session at a fitted bar `k`,
hold across sessions, and flip only on a sufficiently strong contrary signal.

This version keeps that exact low-turnover continuous-holding shape, but adds
two levers the prior family did not use together:

  * an **entry threshold** for the flat state -- very small early returns are
    executed in the full-sample drift direction instead of the fitted
    momentum/reversal direction, so noisy near-zero signals do not put on a
    sizeable position the wrong way
  * **position size is fitted** from a small candidate set, rather than fixed
    at 3, with a turnover penalty to avoid selecting the largest size merely
    because it maximizes in-sample P&L

The fitting procedure therefore searches over `k`, sign convention, entry
threshold quantile, flip threshold quantile, and size.  It simulates the exact
runtime rule -- including the no-trade/flat handling and the flip guard -- and
discards parameter sets whose simulated exposure falls below the registered
exposure floor.  The runtime remains simple and conservative: one 5m decision
per session, market orders only, no stop/target, positions held across sessions.
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Fitted early-session drift with entry threshold and fitted size."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        max_lookback: int = 12,
        min_samples: int = 10,
        sizes: tuple = (1, 2, 3, 4),
        flip_quantiles: tuple = (
            0.0,
            0.1,
            0.2,
            0.25,
            0.3,
            0.4,
            0.5,
            0.6,
            0.7,
            0.8,
            0.9,
            1.0,
        ),
        entry_quantiles: tuple = (0.0, 0.05, 0.10, 0.15, 0.20),
        trade_penalty: float = 0.001,
        exposure_floor: float = 0.70,
    ) -> None:
        self.interval = interval
        self.max_lookback = max_lookback
        self.min_samples = min_samples
        self.sizes = sizes
        self.flip_quantiles = flip_quantiles
        self.entry_quantiles = entry_quantiles
        self.trade_penalty = trade_penalty
        self.exposure_floor = exposure_floor

        # Fitted in setup()
        self._lookback = 1
        self._sign = 1
        self._drift_direction = 1
        self._entry_tau = 0.0
        self._flip_tau = 0.0
        self._size = 1

        # Per-session runtime state
        self._session: int | None = None
        self._bars_this_session = 0
        self._session_bars: list[Any] = []
        self._evaluated_this_session = False

    # ------------------------------------------------------------------
    # Setup / fitting
    # ------------------------------------------------------------------
    def setup(self, ctx) -> None:
        self._fit(ctx)
        ctx.subscribe(self.interval)

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
        """Fit lookback, sign, thresholds and size from training bars."""
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

        min_len = min(len(sessions[sid]) for sid in ordered)
        max_k = min(self.max_lookback, min_len)
        if max_k < 1:
            return

        # Full-sample drift direction, as used by the best prior trial.
        self._drift_direction = 1 if float(bars[-1].close) >= float(bars[0].close) else -1
        final_close = float(sessions[ordered[-1]][-1].close)

        best = None  # (score, contracts, k, sign, entry_tau, flip_tau, size)

        for k in range(1, max_k + 1):
            rows: list[tuple[int, float, float]] = []

            for sid in ordered:
                sb = sessions[sid]
                if len(sb) < k:
                    continue
                first_open = float(sb[0].open)
                close_k = float(sb[k - 1].close)
                early_ret = close_k - first_open
                rows.append((sid, early_ret, close_k))

            if len(rows) < self.min_samples:
                continue

            abs_returns = [abs(r[1]) for r in rows]

            flip_taus = {float("inf")}
            for q in self.flip_quantiles:
                flip_taus.add(self._quantile_abs(abs_returns, q))

            entry_taus = set()
            for q in self.entry_quantiles:
                entry_taus.add(self._quantile_abs(abs_returns, q))

            for entry_tau in sorted(entry_taus):
                for flip_tau in sorted(flip_taus):
                    for sign in (1, -1):
                        for size in self.sizes:
                            pnl, contracts, exposed = self._simulate(
                                rows,
                                sign=sign,
                                drift=self._drift_direction,
                                entry_tau=entry_tau,
                                flip_tau=flip_tau,
                                size=size,
                                final_close=final_close,
                            )
                            score = pnl - self.trade_penalty * contracts

                            # Hard-penalise parameter sets that would produce too
                            # little exposure and therefore not be scored.
                            if exposed < self.exposure_floor * len(rows):
                                score -= 1000.0

                            if (
                                best is None
                                or score > best[0]
                                or (score == best[0] and contracts < best[1])
                            ):
                                best = (score, contracts, k, sign, entry_tau, flip_tau, size)

        if best is not None:
            (
                _score,
                _contracts,
                self._lookback,
                self._sign,
                self._entry_tau,
                self._flip_tau,
                self._size,
            ) = best

    def _simulate(
        self,
        rows: list[tuple[int, float, float]],
        *,
        sign: int,
        drift: int,
        entry_tau: float,
        flip_tau: float,
        size: int,
        final_close: float,
    ) -> tuple[float, int, int]:
        """Simulate the exact continuous-holding runtime rule."""
        pnl = 0.0
        contracts = 0
        exposed = 0
        current = 0
        prev_ref: float | None = None
        eps = 1e-12

        for _sid, early_ret, ref in rows:
            if prev_ref is not None and current != 0:
                pnl += (ref - prev_ref) * current * size

            abs_ret = abs(early_ret)
            desired: int

            if abs_ret < eps:
                # Flat early return: keep existing position, or establish drift
                # if flat.
                desired = drift if current == 0 else current
            else:
                base = sign if early_ret > 0 else -sign

                if current == 0:
                    desired = drift if abs_ret < entry_tau else base
                else:
                    if base != current and abs_ret >= flip_tau:
                        desired = base
                    else:
                        desired = current

            if desired != current:
                contracts += abs(desired - current) * size
                current = desired

            if current != 0:
                exposed += 1

            prev_ref = ref

        if prev_ref is not None and current != 0:
            pnl += (final_close - prev_ref) * current * size

        return pnl, contracts, exposed

    # ------------------------------------------------------------------
    # Runtime
    # ------------------------------------------------------------------
    def _apply_desired(self, ctx, desired: int) -> None:
        """Queue the market order needed to move from current position to desired."""
        pos = int(ctx.position)
        size = int(self._size)

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
        if len(self._session_bars) < self._lookback:
            return

        self._evaluated_this_session = True

        first_open = float(self._session_bars[0].open)
        early_ret = float(bar.close) - first_open
        abs_ret = abs(early_ret)
        eps = 1e-12

        pos = int(ctx.position)
        current = 1 if pos > 0 else -1 if pos < 0 else 0

        if abs_ret < eps:
            desired = self._drift_direction if current == 0 else current
        else:
            base = self._sign if early_ret > 0 else -self._sign

            if current == 0:
                desired = self._drift_direction if abs_ret < self._entry_tau else base
            else:
                if base != current and abs_ret >= self._flip_tau:
                    desired = base
                else:
                    desired = current

        self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
