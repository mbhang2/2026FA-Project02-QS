"""Fitted session-level direction with entry/flip thresholds and cooldown.

The strongest prior shape held one position nearly continuously and used a
fitted early-session return:

    early = close(bar k) - open(bar 0)

to choose direction once per session.  This version generalises that shape in
three ways, all fitted in `setup()` on `ctx.train_bars("5m")`:

1. The first entry is thresholded separately.  When flat, we enter only if
   |early| is at least an entry threshold; otherwise we stay flat and re-evaluate
   on the next session.  This avoids establishing exposure on the weakest
   early-session signals, while the fit requires at least 75% session exposure
   so the run cannot fall under the 70% exposure floor.

2. Flip threshold quantile is searched over several values, not fixed at 0.25.
   A larger flip threshold reduces turnover; a smaller one lets strong contrary
   session-opens flip the position sooner.

3. An optional cooldown (0, 1, or 2 sessions) after any trade suppresses
   repeated whipsaw flips in adjacent sessions.

All parameters are chosen by in-sample continuous-holding backtest over the
training sessions, assuming T+1 fills at the next bar's open and using the same
flatten-and-reverse order sizing the runtime uses.
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Fitted early-session direction, thresholded entry and flips."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 1,
        max_lookback: int = 8,
        min_samples: int = 15,
        min_exposure_frac: float = 0.75,
        fill_penalty: float = 0.0,
        sign_candidates: tuple[int, ...] = (1, -1),
        entry_tau_quantiles: tuple[float, ...] = (0.0, 0.25, 0.4, 0.5, 0.75),
        flip_tau_quantiles: tuple[float, ...] = (
            0.0,
            0.2,
            0.25,
            0.4,
            0.5,
            0.75,
            1.0,
        ),
        cooldown_candidates: tuple[int, ...] = (0, 1, 2),
    ) -> None:
        self.interval = interval
        self.size = size
        self.max_lookback = max_lookback
        self.min_samples = min_samples
        self.min_exposure_frac = min_exposure_frac
        self.fill_penalty = fill_penalty
        self.sign_candidates = sign_candidates
        self.entry_tau_quantiles = entry_tau_quantiles
        self.flip_tau_quantiles = flip_tau_quantiles
        self.cooldown_candidates = cooldown_candidates

        # Fitted in setup()
        self._lookback = 1
        self._sign = 1
        self._entry_tau = 0.0
        self._flip_tau = 0.0
        self._cooldown = 0
        self._drift_direction = 1

        # Session state
        self._session: int | None = None
        self._bars_this_session = 0
        self._session_bars: list[Any] = []
        self._evaluated_this_session = False
        self._last_trade_session: int | None = None

    # ------------------------------------------------------------------
    # Setup / fitting
    # ------------------------------------------------------------------
    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        self._fit(ctx)

    def _fit(self, ctx) -> None:
        """Fit lookback, sign, entry tau, flip tau, and cooldown."""
        try:
            bars = tuple(ctx.train_bars(self.interval))
        except Exception:
            bars = ()

        if not bars:
            self._fallback()
            return

        self._drift_direction = 1 if bars[-1].close >= bars[0].close else -1

        sessions: dict[int, list[Any]] = {}
        for bar in bars:
            sessions.setdefault(bar.session, []).append(bar)

        sorted_sessions = sorted(sessions.keys())
        if not sorted_sessions:
            self._fallback()
            return

        last_sid = sorted_sessions[-1]
        last_close = sessions[last_sid][-1].close
        total_sessions = last_sid + 1  # session indices are 0-based
        min_exposed = max(1, int(self.min_exposure_frac * total_sessions))

        best_net: float | None = None
        best_trades: int | None = None
        best_config: tuple[int, int, float, float, int] | None = None

        for k in range(1, self.max_lookback + 1):
            rows = self._build_rows(k, sessions)
            if len(rows) < self.min_samples:
                continue

            # rows are (session_id, early_return, next_open)
            early_abs = [abs(r[1]) for r in rows]

            for sign in self.sign_candidates:
                for entry_q in self.entry_tau_quantiles:
                    entry_tau = self._quantile_abs(early_abs, entry_q)
                    for flip_q in self.flip_tau_quantiles:
                        flip_tau = self._quantile_abs(early_abs, flip_q)
                        for cooldown in self.cooldown_candidates:
                            pnl, trades, exposed = self._evaluate_rows(
                                rows=rows,
                                last_sid=last_sid,
                                last_close=last_close,
                                sign=sign,
                                entry_tau=entry_tau,
                                flip_tau=flip_tau,
                                cooldown=cooldown,
                            )

                            if exposed < min_exposed:
                                continue

                            net = pnl - self.fill_penalty * trades
                            if best_net is None or net > best_net or (
                                abs(net - best_net) < 1e-12
                                and (best_trades is None or trades < best_trades)
                            ):
                                best_net = net
                                best_trades = trades
                                best_config = (k, sign, entry_tau, flip_tau, cooldown)

        if best_config is None:
            self._fallback()
            return

        (
            self._lookback,
            self._sign,
            self._entry_tau,
            self._flip_tau,
            self._cooldown,
        ) = best_config

    def _fallback(self) -> None:
        self._lookback = 1
        self._sign = 1
        self._entry_tau = 0.0
        self._flip_tau = 0.0
        self._cooldown = 0
        self._drift_direction = 1

    def _build_rows(
        self, k: int, sessions: dict[int, list[Any]]
    ) -> list[tuple[int, float, float]]:
        """Build (sid, early, next_open) rows for lookback k.

        `next_open` is the open of bar k, modelling a T+1 market fill after
        the signal bar closes.
        """
        rows: list[tuple[int, float, float]] = []
        for sid in sorted(sessions.keys()):
            sb = sessions[sid]
            if len(sb) < k + 1:
                continue
            early = sb[k - 1].close - sb[0].open
            next_open = sb[k].open
            rows.append((sid, early, next_open))
        return rows

    def _quantile_abs(self, values: list[float], q: float) -> float:
        if q <= 0.0:
            return 0.0
        if q >= 1.0:
            return float("inf")

        vals = sorted(abs(v) for v in values)
        if not vals:
            return 0.0
        idx = int(q * len(vals))
        if idx >= len(vals):
            idx = len(vals) - 1
        return vals[idx]

    def _direction_for_early(self, early: float, sign: int, fallback: int) -> int:
        """Desired +1/-1 for a non-zero early return, reverting to fallback."""
        if early > 1e-12:
            return sign
        if early < -1e-12:
            return -sign
        return fallback

    def _evaluate_rows(
        self,
        rows: list[tuple[int, float, float]],
        last_sid: int,
        last_close: float,
        sign: int,
        entry_tau: float,
        flip_tau: float,
        cooldown: int,
    ) -> tuple[float, int, int]:
        """Simulate continuous holding on training rows.

        Mirrors the runtime: whether and when to establish a first position is
        controlled by `entry_tau`; subsequent session signals are flips only
        when the contrary signal is at least `flip_tau` in magnitude and any
        cooldown has elapsed.  Entry/flip fills are at the next bar's open.
        """
        pnl = 0.0
        trades = 0
        holding = False
        current = 0
        entry = 0.0
        entry_sid: int | None = None
        last_trade_sid: int | None = None

        for sid, early, next_open in rows:
            if not holding:
                if abs(early) >= entry_tau:
                    desired = self._direction_for_early(
                        early, sign, self._drift_direction
                    )
                else:
                    desired = 0

                if desired != 0:
                    holding = True
                    current = desired
                    entry = next_open
                    trades += 1
                    entry_sid = sid
                    last_trade_sid = sid
                continue

            # Already holding: decide if a contrary signal is strong enough
            # to justify a flatten-and-reverse fill.
            if abs(early) >= flip_tau:
                new_desired = self._direction_for_early(early, sign, current)
            else:
                new_desired = current

            if new_desired != current:
                cooldown_ok = (
                    cooldown <= 0
                    or last_trade_sid is None
                    or (sid - last_trade_sid > cooldown)
                )
                if cooldown_ok:
                    pnl += (next_open - entry) * current
                    current = new_desired
                    entry = next_open
                    trades += 1
                    last_trade_sid = sid

        if holding:
            pnl += (last_close - entry) * current
            exposed = last_sid - entry_sid + 1 if entry_sid is not None else 0
        else:
            exposed = 0

        return pnl, trades, exposed

    # ------------------------------------------------------------------
    # Runtime helpers
    # ------------------------------------------------------------------
    def _apply_desired(self, ctx, desired: int) -> None:
        """Queue the market order needed to move from position to desired."""
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
        elif desired < 0:
            if pos == 0:
                ctx.sell(size=self.size, tif="DAY", tag="session-short")
            elif pos > 0:
                ctx.sell(
                    size=pos + self.size,
                    tif="DAY",
                    tag="session-flip-short",
                )

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------
    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return

        bar = ctx.bars[self.interval]

        # Reset per-session state on session change.
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
        early = bar.close - first_open

        pos = ctx.position
        current = 1 if pos > 0 else -1 if pos < 0 else 0

        if current == 0:
            if abs(early) >= self._entry_tau:
                desired = self._direction_for_early(
                    early, self._sign, self._drift_direction
                )
            else:
                desired = 0

            if desired != 0:
                self._apply_desired(ctx, desired)
                self._last_trade_session = ctx.session
            return

        # Already holding: flip only on a large contrary signal.
        if abs(early) >= self._flip_tau:
            desired = self._direction_for_early(early, self._sign, current)
        else:
            desired = current

        if desired != current:
            cooldown_ok = (
                self._cooldown <= 0
                or self._last_trade_session is None
                or (ctx.session - self._last_trade_session > self._cooldown)
            )
            if cooldown_ok:
                self._apply_desired(ctx, desired)
                self._last_trade_session = ctx.session

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
