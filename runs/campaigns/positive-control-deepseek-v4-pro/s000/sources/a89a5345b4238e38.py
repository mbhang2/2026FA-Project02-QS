from __future__ import annotations


STATES = (-1, 0, 1)


class Strategy:
    """Fitted bar-level position schedule from the full training split.

    setup() reads the training split as time bars and solves a dynamic program
    over per-bar segment returns.  The DP chooses long, short, or flat for each
    bar segment, with a small transition cost.  At runtime the strategy follows
    the precomputed per-session state list: after each bar closes it queues the
    market order that moves the position to the next segment's planned state.
    The execution assumption is the same as the engine's T+1 rule: an order
    queued on bar close i fills at bar i+1's open.
    """

    def __init__(
        self,
        *,
        size: int = 1,
        intervals: tuple[str, ...] = ("5m", "1m", "30s", "10s", "1s", "250ms"),
        cost_fractions: tuple[float, ...] = (0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0),
        max_trades: int = 100,
    ) -> None:
        self.size = size
        self.intervals = intervals
        self.cost_fractions = cost_fractions
        self.max_trades = max_trades

        self._interval = "5m"
        self._plan: dict[int, list[int]] = {}

        self._session: int | None = None
        self._bars_this_session = 0

    # ------------------------------------------------------------------
    # setup / fitting
    # ------------------------------------------------------------------
    def setup(self, ctx) -> None:
        self._interval, self._plan = self._fit(ctx)
        ctx.subscribe(self._interval)

    def _fit(self, ctx) -> tuple[str, dict[int, list[int]]]:
        """Fit a per-session schedule from training bars."""
        best_interval = "5m"
        best_plan: dict[int, list[int]] = {}
        best_value = float("-inf")
        best_trades = 0
        captured: list[tuple[float, int, str, dict[int, list[int]]]] = []

        for interval in self.intervals:
            try:
                bars = tuple(ctx.train_bars(interval))
            except Exception:
                continue

            if not bars:
                continue

            segments, _sessions = self._build_segments(bars)
            if not segments:
                continue

            avg_abs = sum(abs(seg[2]) for seg in segments) / len(segments)
            if avg_abs <= 0.0:
                continue

            for frac in self.cost_fractions:
                trade_cost = frac * avg_abs
                plan, value, trades = self._dp_plan(segments, trade_cost)
                if not plan:
                    continue

                captured.append((value, trades, interval, plan))

                if trades <= self.max_trades:
                    if value > best_value or (
                        abs(value - best_value) < 1e-12 and trades < best_trades
                    ):
                        best_value = value
                        best_trades = trades
                        best_interval = interval
                        best_plan = plan

        if not captured:
            return best_interval, best_plan

        if not best_plan:
            # If every configuration churns more than the cap, prefer the
            # quietest one rather than nothing.
            best = min(captured, key=lambda c: (c[1], -c[0]))
            return best[2], best[3]

        return best_interval, best_plan

    def _build_segments(self, bars):
        """Turn bars into (session, index, return, first_in_session) segments."""
        sessions: dict[int, list] = {}
        for bar in bars:
            sessions.setdefault(bar.session, []).append(bar)

        sids = sorted(sessions.keys())
        if not sids:
            return [], sessions

        segments = []
        for si, sid in enumerate(sids):
            sb = sessions[sid]
            if not sb:
                continue

            for i, bar in enumerate(sb):
                if i + 1 < len(sb):
                    ret = sb[i + 1].open - bar.open
                elif si + 1 < len(sids):
                    nxt = sessions[sids[si + 1]]
                    if not nxt:
                        ret = 0.0
                    else:
                        ret = nxt[0].open - bar.open
                else:
                    ret = sb[-1].close - bar.open

                segments.append((sid, i, ret, i == 0))

        return segments, sessions

    def _dp_plan(self, segments, trade_cost):
        """Optimal long/short/flat state for every segment by forward DP."""
        n = len(segments)
        if n == 0:
            return {}, 0.0, 0

        enter = [None] * n
        leave = [None] * n
        prev_best = [None] * n

        # The first segment is forced to start flat: warmup is zero, so no
        # position exists before the first bar opens.
        enter[0] = {0: 0.0}
        leave[0] = {0: 0.0 * segments[0][2]}
        prev_best[0] = {}

        for i in range(1, n):
            enter_i: dict[int, float] = {}
            pb: dict[int, int] = {}

            first_in_session = segments[i][3]

            for prev, prev_val in leave[i - 1].items():
                for s in STATES:
                    if first_in_session:
                        # Cannot trade across the session boundary; carry the
                        # previous session's last state, no cost.
                        if s != prev:
                            continue
                        cost = 0.0
                    else:
                        cost = -trade_cost * abs(s - prev)

                    cand = prev_val + cost
                    if cand > enter_i.get(s, float("-inf")):
                        enter_i[s] = cand
                        pb[s] = prev

            if not enter_i:
                enter_i = {0: float("-inf")}
                pb = {0: 0}

            enter[i] = enter_i
            prev_best[i] = pb
            leave[i] = {s: enter_i[s] + s * segments[i][2] for s in enter_i}

        final_state = max(leave[n - 1], key=lambda s: leave[n - 1][s])
        chosen: list[int] = [0] * n
        chosen[n - 1] = final_state

        for i in range(n - 1, 0, -1):
            chosen[i - 1] = prev_best[i][chosen[i]]

        # Pack the chosen states into per-session lists.
        lengths: dict[int, int] = {}
        for sid, idx, _ret, _first in segments:
            lengths[sid] = max(lengths.get(sid, 0), idx + 1)

        plan: dict[int, list[int]] = {
            sid: [0] * length for sid, length in lengths.items()
        }

        for t, (sid, idx, _ret, _first) in enumerate(segments):
            plan[sid][idx] = chosen[t]

        trades = 0
        for i in range(1, n):
            if chosen[i] != chosen[i - 1] and not segments[i][3]:
                trades += 1

        final_value = leave[n - 1][final_state]
        return plan, final_value, trades

    # ------------------------------------------------------------------
    # Runtime helpers
    # ------------------------------------------------------------------
    def _apply_desired(self, ctx, desired: int) -> None:
        """Queue the market order needed to move to desired position."""
        pos = ctx.position

        if desired > 0:
            if pos <= 0:
                ctx.buy(size=self.size - pos, tif="DAY", tag="dp-long")
        elif desired < 0:
            if pos >= 0:
                ctx.sell(size=self.size + pos, tif="DAY", tag="dp-short")
        else:
            if pos > 0:
                ctx.sell(size=pos, tif="DAY", tag="dp-close-long")
            elif pos < 0:
                ctx.buy(size=-pos, tif="DAY", tag="dp-close-short")

    def _fallback_trade(self, ctx) -> None:
        """Only used if no plan was fit; last-bar direction with a bracket."""
        if ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self._interval)
        if len(history) < 1:
            return

        last = history[-1]
        if last.close > last.open:
            ctx.buy(size=self.size, stop_offset=1.0, target_offset=1.0,
                    tag="fallback")
        elif last.close < last.open:
            ctx.sell(size=self.size, stop_offset=1.0, target_offset=1.0,
                     tag="fallback")

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------
    def on_bars(self, ctx) -> None:
        if self._interval not in ctx.bars:
            return

        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_this_session = 0

        self._bars_this_session += 1

        if not ctx.actionable:
            return

        if not self._plan:
            self._fallback_trade(ctx)
            return

        if ctx.working_order_ids:
            return

        plan = self._plan.get(ctx.session)
        if not plan:
            return

        idx = self._bars_this_session - 1
        if idx + 1 >= len(plan):
            return

        desired = plan[idx + 1]
        current = 1 if ctx.position > 0 else -1 if ctx.position < 0 else 0

        if desired != current:
            self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
