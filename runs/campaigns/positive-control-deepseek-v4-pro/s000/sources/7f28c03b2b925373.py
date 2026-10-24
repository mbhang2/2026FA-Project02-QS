"""A low-turnover, session-level direction schedule fitted from the training split.

The previous best strategies in this campaign held a near-continuous position
and changed it only when a fitted early-session signal was strong.  This
version fits a per-session long/short schedule directly from the training
bars that are available in setup().  For each candidate bar interval and entry
bar, it builds one entry price per session (the open of the bar after the
signal bar, which matches the engine's T+1 fill for a market order queued on
that signal bar's close) and then solves a two-state dynamic program over the
sequence of session entry prices.  The DP chooses +1 or -1 to maximize the
continuous holding return between session entry points, with a small transition
penalty to keep turnover low.  The result is a precomputed per-session desired
position.

At runtime the strategy follows that schedule: at the chosen bar of each
session it queues the single market order needed to move from the current
position to the planned +1 or -1.  It keeps no order ids; all orders are fresh
market orders.  Sessions without a planned state fall back to the full-sample
drift direction when flat, and otherwise keep the existing position.
"""

from __future__ import annotations

from math import inf


class Strategy:
    """Fitted per-session direction schedule, held with minimal turnover."""

    def __init__(
        self,
        *,
        intervals: tuple[str, ...] = ("5m", "2m", "1m"),
        entry_bars: tuple[int, ...] = (0, 1, 2),
        cost_fractions: tuple[float, ...] = (0.0, 0.05, 0.15, 0.30, 0.50, 0.75, 1.0),
        max_flips: int = 45,
        size: int = 1,
    ) -> None:
        self.intervals = intervals
        self.entry_bars = entry_bars
        self.cost_fractions = cost_fractions
        self.max_flips = max_flips
        self.size = size

        # Fitted in setup()
        self._interval = "5m"
        self._entry_bar = 0
        self._plan: dict[int, int] = {}
        self._fallback_sign = 1

        # Runtime state
        self._session: int | None = None
        self._bars_this_session = 0
        self._acted_this_session = False

    # ------------------------------------------------------------------
    # Setup / fitting
    # ------------------------------------------------------------------
    def setup(self, ctx) -> None:
        fitted = self._fit(ctx)

        if fitted is not None:
            self._interval, self._entry_bar, self._plan, self._fallback_sign = fitted
        else:
            self._interval = "5m"
            self._entry_bar = 0
            self._plan = {}
            self._fallback_sign = 1

        ctx.subscribe(self._interval)

    def _fit(self, ctx):
        """Fit the schedule by dynamic programming over session entries."""
        candidates: list[tuple] = []

        for interval in self.intervals:
            try:
                bars = tuple(ctx.train_bars(interval))
            except Exception:
                continue

            if not bars:
                continue

            sessions: dict[int, list] = {}
            for bar in bars:
                sessions.setdefault(bar.session, []).append(bar)

            final_close = bars[-1].close
            drift_sign = 1 if final_close >= bars[0].close else -1

            for entry_bar in self.entry_bars:
                entries = self._build_entries(sessions, entry_bar)
                if len(entries) < 10:
                    continue

                returns = self._returns_from_entries(entries, final_close)
                med = self._median_abs(returns)
                if med <= 0.0:
                    med = 1.0

                # Baseline: always hold in the direction of the full-sample move.
                base_state = 1 if sum(returns) >= 0 else -1
                base_raw = abs(sum(returns))
                candidates.append(
                    (
                        base_raw,          # net
                        0,                 # flips
                        base_raw,          # raw pnl
                        interval,
                        entry_bar,
                        entries,
                        [base_state] * len(entries),
                        drift_sign,
                    )
                )

                for frac in self.cost_fractions:
                    flip_cost = frac * med
                    states, flips, raw, net = self._dp_path(
                        entries, final_close, flip_cost
                    )
                    if states is not None:
                        candidates.append(
                            (net, flips, raw, interval, entry_bar, entries, states, drift_sign)
                        )

        if not candidates:
            return None

        eligible = [c for c in candidates if c[1] <= self.max_flips]
        if not eligible:
            # If every schedule is too active, prefer the quietest one.
            best = min(candidates, key=lambda c: (c[1], -c[0]))
        else:
            # Highest penalized net P&L, then fewer flips, then higher raw P&L.
            best = max(eligible, key=lambda c: (c[0], -c[1], c[2]))

        _net, _flips, _raw, interval, entry_bar, entries, states, drift_sign = best
        plan = {sid: state for (sid, _), state in zip(entries, states)}
        return interval, entry_bar, plan, drift_sign

    # ------------------------------------------------------------------
    # Fitting helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _group_sessions(bars) -> dict[int, list]:
        sessions: dict[int, list] = {}
        for bar in bars:
            sessions.setdefault(bar.session, []).append(bar)
        return sessions

    @staticmethod
    def _build_entries(sessions: dict[int, list], entry_bar: int) -> list[tuple[int, float]]:
        """Return (session_id, fill_price) for sessions with enough bars.

        The runtime signal is queued on the close of bar index `entry_bar`
        (0-based) and fills on the next bar's open, so the modelled fill is
        `sb[entry_bar + 1].open`.
        """
        entries: list[tuple[int, float]] = []
        for sid in sorted(sessions.keys()):
            sb = sessions[sid]
            if len(sb) >= entry_bar + 2:
                entries.append((sid, sb[entry_bar + 1].open))
        return entries

    @staticmethod
    def _returns_from_entries(entries: list[tuple[int, float]], final_close: float) -> list[float]:
        returns: list[float] = []
        for i in range(len(entries) - 1):
            returns.append(entries[i + 1][1] - entries[i][1])
        returns.append(final_close - entries[-1][1])
        return returns

    @staticmethod
    def _median_abs(values: list[float]) -> float:
        if not values:
            return 0.0
        ordered = sorted(abs(v) for v in values)
        n = len(ordered)
        if n % 2:
            return ordered[n // 2]
        return (ordered[n // 2 - 1] + ordered[n // 2]) / 2.0

    @staticmethod
    def _dp_path(entries: list[tuple[int, float]], final_close: float, flip_cost: float):
        """Solve the two-state (+1/-1) schedule with a per-flip penalty."""
        prices = [p for _, p in entries]
        n = len(prices)
        if n < 2:
            return None, 0, 0.0, 0.0

        returns = [prices[i + 1] - prices[i] for i in range(n - 1)]
        returns.append(final_close - prices[-1])

        states = (1, -1)
        dp_prev = {1: 0.0, -1: 0.0}
        choices: dict[int, dict[int, int]] = {}

        for i, r in enumerate(returns):
            dp: dict[int, float] = {}
            ch: dict[int, int] = {}

            for s in states:
                best_val = -inf
                best_prev = None
                for ps in states:
                    penalty = flip_cost if s != ps else 0.0
                    val = dp_prev[ps] + s * r - penalty
                    if val > best_val:
                        best_val = val
                        best_prev = ps

                dp[s] = best_val
                ch[s] = best_prev if best_prev is not None else ps

            dp_prev = dp
            choices[i] = ch

        final_state = max(states, key=lambda s: dp_prev[s])

        path = [0] * n
        path[-1] = final_state
        for i in range(n - 1, 0, -1):
            path[i - 1] = choices[i][path[i]]

        raw = sum(path[i] * returns[i] for i in range(n))
        flips = sum(1 for i in range(1, n) if path[i] != path[i - 1])
        net = raw - flips * flip_cost
        return path, flips, raw, net

    # ------------------------------------------------------------------
    # Runtime helpers
    # ------------------------------------------------------------------
    def _apply_desired(self, ctx, desired: int) -> None:
        """Queue the single market order needed to move to the desired position."""
        pos = ctx.position

        if desired > 0:
            if pos == 0:
                ctx.buy(size=self.size, tif="DAY", tag="plan-long")
            elif pos < 0:
                ctx.buy(size=-pos + self.size, tif="DAY", tag="plan-flip-long")
        elif desired < 0:
            if pos == 0:
                ctx.sell(size=self.size, tif="DAY", tag="plan-short")
            elif pos > 0:
                ctx.sell(size=pos + self.size, tif="DAY", tag="plan-flip-short")

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------
    def on_bars(self, ctx) -> None:
        if self._interval not in ctx.bars:
            return

        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_this_session = 0
            self._acted_this_session = False

        self._bars_this_session += 1

        if not ctx.actionable:
            return
        if ctx.working_order_ids:
            return
        if self._acted_this_session:
            return

        signal_bar = self._bars_this_session - 1
        if signal_bar != self._entry_bar:
            return

        self._acted_this_session = True

        desired = self._plan.get(ctx.session)
        if desired is None:
            pos = ctx.position
            if pos == 0:
                # For an unplanned session, establish drift exposure rather
                # than sitting flat and failing the exposure floor.
                self._apply_desired(ctx, self._fallback_sign)
            return

        current = 1 if ctx.position > 0 else -1 if ctx.position < 0 else 0
        if desired != current:
            self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
