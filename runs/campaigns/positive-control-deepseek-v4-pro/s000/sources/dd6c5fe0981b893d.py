"""A fitted early-session drift strategy with a blended price/order-flow signal.

The best prior campaigns (#60, #61, #68, #87) all used one decision per
session, taken at a fitted bar k, then held across sessions with low turnover.
This version keeps that shape but widens the signal class:

    raw = sign * (ret_norm + w_delta * delta_norm + w_rp * range_position)

where
    ret_norm      = (close_k - open_0) / range_k
    delta_norm    = sum(delta over first k bars) / sum(volume over first k bars)
    range_position = (close_k - low_k) / range_k - 0.5

The lookback k, sign, weights, and two thresholds (entry when flat, flip when
already positioned) are all fitted on training 5m bars by simulating the exact
live continuous-holding decision rule and maximizing mark-to-market P&L, with
fewer trades used as a tie-breaker.  If no signal is available the strategy
falls back to the full-sample drift direction, so it is never left permanently
flat and never reaches the exposure floor from zero trades.

The live block is deliberately conservative: only closed bars, no tick
subscription, `ctx.actionable` checked before every order, and all orders are
tagged rather than tracked by id.
"""

from __future__ import annotations


class Strategy:
    """Blended early-session signal, held across sessions with low turnover."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 3,
        max_lookback: int = 8,
        min_samples: int = 10,
        delta_weights: tuple[float, ...] = (-1.0, -0.5, 0.0, 0.5, 1.0, 2.0),
        rp_weights: tuple[float, ...] = (-0.5, 0.0, 0.5, 1.0),
        entry_taus: tuple[float, ...] = (0.0, 0.05, 0.1, 0.2),
        flip_taus: tuple[float, ...] = (0.0, 0.05, 0.1, 0.2, float("inf")),
    ) -> None:
        self.interval = interval
        self.size = size
        self.max_lookback = max_lookback
        self.min_samples = min_samples
        self.delta_weights = delta_weights
        self.rp_weights = rp_weights
        self.entry_taus = entry_taus
        self.flip_taus = flip_taus

        # Fitted in setup()
        self._lookback = 1
        self._sign = 1
        self._delta_weight = 0.0
        self._rp_weight = 0.0
        self._entry_tau = 0.0
        self._flip_tau = 0.0
        self._drift_direction = 1

        # Per-session runtime state
        self._session: int | None = None
        self._bars_this_session = 0
        self._session_bars: list = []
        self._evaluated_this_session = False

    # ------------------------------------------------------------------
    # Setup / fitting
    # ------------------------------------------------------------------
    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        self._fit(ctx)

    def _fit(self, ctx) -> None:
        """Fit lookback, signal weights, sign, and thresholds from train bars."""
        try:
            bars = tuple(ctx.train_bars(self.interval))
        except Exception:
            bars = ()

        if not bars:
            return

        self._drift_direction = 1 if float(bars[-1].close) >= float(bars[0].close) else -1
        final_close = float(bars[-1].close)

        sessions: dict[int, list] = {}
        for bar in bars:
            sessions.setdefault(bar.session, []).append(bar)

        ordered = sorted(sessions.keys())
        if len(ordered) < self.min_samples:
            return

        max_k = min(self.max_lookback, min(len(sessions[s]) for s in ordered))
        if max_k < 1:
            return

        best_score: float | None = None
        best_trades: int | None = None
        best_params = {
            "lookback": 1,
            "sign": 1,
            "delta_weight": 0.0,
            "rp_weight": 0.0,
            "entry_tau": 0.0,
            "flip_tau": 0.0,
        }

        for k in range(1, max_k + 1):
            # Precompute normalized features at bar k for each session with
            # enough bars.  Signals for a candidate are then a cheap linear
            # combination of these.
            features = []
            for sid in ordered:
                sb = sessions[sid]
                if len(sb) < k:
                    continue
                first_k = sb[:k]
                open0 = float(first_k[0].open)
                close_k = float(first_k[-1].close)
                high = max(float(b.high) for b in first_k)
                low = min(float(b.low) for b in first_k)
                rng = high - low
                if rng <= 1e-12:
                    ret_norm = 0.0
                    rp = 0.5
                else:
                    ret_norm = (close_k - open0) / rng
                    rp = (close_k - low) / rng

                total_volume = sum(float(b.volume) for b in first_k)
                total_delta = sum(float(b.delta) for b in first_k)
                delta_norm = (
                    total_delta / total_volume if total_volume > 1e-12 else 0.0
                )

                features.append((sid, ret_norm, delta_norm, rp, close_k))

            if len(features) < self.min_samples:
                continue

            for w_delta in self.delta_weights:
                for w_rp in self.rp_weights:
                    for sign in (1, -1):
                        signals = []
                        for sid, ret_norm, delta_norm, rp, close_k in features:
                            raw = sign * (
                                ret_norm
                                + w_delta * delta_norm
                                + w_rp * (rp - 0.5)
                            )
                            signals.append((sid, raw, close_k))

                        for entry_tau in self.entry_taus:
                            for flip_tau in self.flip_taus:
                                pnl, trades = self._simulate(
                                    signals,
                                    entry_tau=entry_tau,
                                    flip_tau=flip_tau,
                                    drift=self._drift_direction,
                                    final_close=final_close,
                                )

                                if best_score is None or pnl > best_score or (
                                    abs(pnl - best_score) <= 1e-12
                                    and (best_trades is None or trades < best_trades)
                                ):
                                    best_score = pnl
                                    best_trades = trades
                                    best_params = {
                                        "lookback": k,
                                        "sign": sign,
                                        "delta_weight": w_delta,
                                        "rp_weight": w_rp,
                                        "entry_tau": entry_tau,
                                        "flip_tau": flip_tau,
                                    }

        if best_score is not None:
            self._lookback = int(best_params["lookback"])
            self._sign = int(best_params["sign"])
            self._delta_weight = best_params["delta_weight"]
            self._rp_weight = best_params["rp_weight"]
            self._entry_tau = best_params["entry_tau"]
            self._flip_tau = best_params["flip_tau"]

    def _simulate(
        self,
        signals: list[tuple[int, float, float]],
        *,
        entry_tau: float,
        flip_tau: float,
        drift: int,
        final_close: float,
    ) -> tuple[float, int]:
        """Simulate the actual live continuous-holding decision rule."""
        pnl = 0.0
        trades = 0
        current = 0
        prev_close: float | None = None

        for _sid, signal, close_k in signals:
            if prev_close is not None and current != 0:
                pnl += (close_k - prev_close) * current

            if abs(signal) < 1e-12:
                if current == 0:
                    current = drift
                    trades += 1
                prev_close = close_k
                continue

            desired = 1 if signal > 0 else -1

            if current == 0:
                target = desired if abs(signal) >= entry_tau else drift
                if target != current:
                    current = target
                    trades += 1
            elif current != desired:
                if abs(signal) >= flip_tau:
                    current = desired
                    trades += 1

            prev_close = close_k

        if prev_close is not None and current != 0:
            pnl += (final_close - prev_close) * current

        return pnl, trades

    # ------------------------------------------------------------------
    # Runtime
    # ------------------------------------------------------------------
    def _apply_desired(self, ctx, desired: int) -> None:
        """Queue the market order needed to move from current position to desired."""
        pos = ctx.position
        size = self.size

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
        if self._bars_this_session <= self._lookback:
            self._session_bars.append(bar)

        if not ctx.actionable:
            return
        if ctx.working_order_ids:
            return
        if self._evaluated_this_session or self._bars_this_session != self._lookback:
            return

        self._evaluated_this_session = True

        first_k = self._session_bars
        if len(first_k) < self._lookback:
            return

        open0 = float(first_k[0].open)
        close_k = float(first_k[-1].close)
        high = max(float(b.high) for b in first_k)
        low = min(float(b.low) for b in first_k)
        rng = high - low

        if rng <= 1e-12:
            ret_norm = 0.0
            rp = 0.5
        else:
            ret_norm = (close_k - open0) / rng
            rp = (close_k - low) / rng

        total_volume = sum(float(b.volume) for b in first_k)
        total_delta = sum(float(b.delta) for b in first_k)
        delta_norm = total_delta / total_volume if total_volume > 1e-12 else 0.0

        raw = self._sign * (
            ret_norm + self._delta_weight * delta_norm + self._rp_weight * (rp - 0.5)
        )

        pos = ctx.position
        current = 1 if pos > 0 else -1 if pos < 0 else 0

        if abs(raw) < 1e-12:
            if current == 0:
                self._apply_desired(ctx, self._drift_direction)
            return

        desired = 1 if raw > 0 else -1

        if current == 0:
            target = desired if abs(raw) >= self._entry_tau else self._drift_direction
            self._apply_desired(ctx, target)
        elif current != desired:
            if abs(raw) >= self._flip_tau:
                self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
