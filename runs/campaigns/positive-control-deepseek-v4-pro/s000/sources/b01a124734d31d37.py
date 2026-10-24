"""Next strategy: expand the best prior family to search over interval, feature,
decision bar, sign and flip threshold in setup.

The best prior trials (#60/#61) fitted an early-session drift on 5-minute bars,
one decision per session, then held across sessions with size=3.  This keeps
that exact runtime shape but makes the fitting step broader:

  * candidate intervals: 15s, 30s, 1m, 2m, 5m, 10m
  * candidate early-session features: price return from first bar open to
    decision-bar close, or signed volume imbalance over the same window
  * decision bar k selected by an in-sample continuous-holding simulation
  * flip threshold chosen only from high quantiles plus "never flip" (inf),
    preserving the low-turnover character of the proven family

The runtime remains simple: subscribe to the chosen interval, accumulate bars
on self so no history-window bound is involved, make one decision per session,
and use only market orders with no stop/target.  Positions are held across
sessions, exactly like the best prior trials.
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Fitted early-session drift, with broadened parameter search."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 3,
        max_lookback: int = 20,
        min_samples: int = 15,
        feature_types: tuple = ("ret", "delta"),
        trade_penalty: float = 0.0,
    ) -> None:
        self.interval = interval
        self.size = size
        self.max_lookback = max_lookback
        self.min_samples = min_samples
        self.feature_types = feature_types
        self.trade_penalty = trade_penalty

        # Fitted in setup()
        self._lookback = 1
        self._sign = 1
        self._drift_direction = 1
        self._flip_tau = 0.0
        self._feature = "ret"

        # Per-session runtime state
        self._session: int | None = None
        self._bars_this_session = 0
        self._session_bars: list[Any] = []
        self._evaluated_this_session = False

    def setup(self, ctx) -> None:
        self._fit(ctx)
        ctx.subscribe(self.interval)

    # ------------------------------------------------------------------
    # Fitting
    # ------------------------------------------------------------------
    def _fit(self, ctx) -> None:
        candidate_intervals = ("15s", "30s", "1m", "2m", "5m", "10m")
        best = None  # (pnl, contracts, interval, k, feature, sign, tau, drift)

        for interval in candidate_intervals:
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

            min_len = min(len(sessions[sid]) for sid in ordered)
            if min_len < 2:
                continue

            max_k = min(self.max_lookback, min_len - 1)
            if max_k < 1:
                continue

            drift = 1 if bars[-1].close >= bars[0].close else -1
            final_close = float(sessions[ordered[-1]][-1].close)

            for k in range(1, max_k + 1):
                for feature in self.feature_types:
                    rows = self._make_rows(sessions, ordered, k, feature)
                    if len(rows) < self.min_samples:
                        continue

                    abs_vals = sorted(abs(r[1]) for r in rows)
                    taus = set()
                    # High quantiles only, plus never-flip.  This keeps turnover
                    # low and preserves the character of the best prior family.
                    for q in (0.25, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0):
                        idx = int(q * len(abs_vals))
                        if idx >= len(abs_vals):
                            idx = len(abs_vals) - 1
                        taus.add(abs_vals[idx])
                    taus.add(float("inf"))

                    for sign in (1, -1):
                        for tau in sorted(taus):
                            pnl, contracts = self._simulate(
                                rows,
                                sign=sign,
                                drift=drift,
                                tau=tau,
                                final_close=final_close,
                            )
                            score = pnl - self.trade_penalty * contracts

                            if (
                                best is None
                                or score > best[0]
                                or (
                                    score == best[0]
                                    and contracts < best[1]
                                )
                            ):
                                best = (
                                    score,
                                    contracts,
                                    interval,
                                    k,
                                    feature,
                                    sign,
                                    tau,
                                    drift,
                                )

        if best is not None:
            (
                _,
                _,
                self.interval,
                self._lookback,
                self._feature,
                self._sign,
                self._flip_tau,
                self._drift_direction,
            ) = best

    def _make_rows(
        self,
        sessions: dict[int, list[Any]],
        ordered: list[int],
        k: int,
        feature: str,
    ) -> list[tuple[int, float, float]]:
        """Build one row per session: (sid, early_feature, next_bar_open)."""
        rows: list[tuple[int, float, float]] = []
        for sid in ordered:
            sb = sessions[sid]
            if len(sb) < k + 1:
                continue

            first_open = float(sb[0].open)
            decision_bar = sb[k - 1]

            val: float
            if feature == "delta":
                val = float(
                    sum(
                        int(b.buy_volume) - int(b.sell_volume)
                        for b in sb[:k]
                    )
                )
            else:
                val = float(decision_bar.close) - first_open

            ref = float(sb[k].open) if len(sb) > k else float(decision_bar.close)
            rows.append((sid, val, ref))
        return rows

    def _simulate(
        self,
        rows: list[tuple[int, float, float]],
        *,
        sign: int,
        drift: int,
        tau: float,
        final_close: float,
    ) -> tuple[float, int]:
        """Continuous-holding mark-to-market simulation."""
        pnl: float = 0.0
        current: int = 0
        prev_ref: float | None = None
        contracts: int = 0
        eps = 1e-12

        for _sid, feature_value, ref in rows:
            if prev_ref is not None and current != 0:
                pnl += (ref - prev_ref) * current * self.size

            abs_val = abs(feature_value)

            desired: int
            if abs_val < eps:
                desired = drift if current == 0 else current
            else:
                base = sign if feature_value > 0 else -sign

                if current == 0:
                    desired = base
                elif current != base:
                    if abs_val >= tau:
                        desired = base
                    else:
                        desired = current
                else:
                    desired = current

            if desired != current:
                contracts += abs(desired - current) * self.size
                current = desired

            prev_ref = ref

        if prev_ref is not None and current != 0:
            pnl += (final_close - prev_ref) * current * self.size

        return pnl, contracts

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
        feature_value: float
        if self._feature == "delta":
            feature_value = float(
                sum(
                    int(b.buy_volume) - int(b.sell_volume)
                    for b in self._session_bars[: self._lookback]
                )
            )
        else:
            feature_value = float(bar.close) - first_open

        abs_val = abs(feature_value)
        pos = int(ctx.position)
        current = 1 if pos > 0 else -1 if pos < 0 else 0

        if abs_val < 1e-12:
            # Flat early signal: establish full-sample drift when flat,
            # otherwise leave the existing position alone.
            if current == 0:
                self._apply_desired(ctx, self._drift_direction)
            return

        base = self._sign if feature_value > 0 else -self._sign

        if current == 0:
            self._apply_desired(ctx, base)
        elif current != base:
            if abs_val >= self._flip_tau:
                self._apply_desired(ctx, base)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
