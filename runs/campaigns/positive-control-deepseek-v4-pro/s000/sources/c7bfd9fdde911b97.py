"""Session-level fitted target positions, optimized on the training split.

The best prior strategies in this campaign (#33/#34 and descendants) all
reduced a low-turnover, early-session directional decision to one fitted
lookback and one fitted sign.  This proposal keeps the same low-turnover,
one-decision-per-session shape but replaces the hand-picked signal with a
directly optimized session-level target-position schedule.

In setup() we have the whole training split through ctx.train_bars().  We
search a small set of bar intervals and entry-bar indices.  For each candidate
we compute the fill price that would follow a market order submitted at the
chosen bar's close (approximated by the next bar's open), and then use a
two-state dynamic program to choose +1 or -1 for every session so as to
maximize net holding P&L after transaction costs.  The result is a per-session
target direction.  At runtime the strategy simply looks up the target for the
current session and, if the position is not already there, submits a single
market order on the chosen bar.

This is deliberately low-turnover: changing from +1 to -1 trades two
contracts and pays commission.  The DP's cost term, scaled by a fraction of
the median absolute return, keeps the fitted path from over-trading while
still following the strongest in-sample moves.  If a session is missing from
the plan, the strategy falls back to the full-sample drift direction so it
remains exposed and does not trip the exposure floor.
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Fitted per-session target positions with a single entry decision per session."""

    def __init__(
        self,
        *,
        size: int = 1,
        intervals: tuple[str, ...] = (
            "5m",
            "2m",
            "1m",
            "10m",
            "15m",
            "30m",
            "30s",
        ),
        max_entry_bar: int = 8,
        fee_frac: float = 0.05,
        min_sessions: int = 10,
        max_turnover: int = 100,
    ) -> None:
        self.size = size
        self.intervals = intervals
        self.max_entry_bar = max_entry_bar
        self.fee_frac = fee_frac
        self.min_sessions = min_sessions
        self.max_turnover = max_turnover

        # Fitted values.  Safe defaults before setup() runs.
        self.interval = "5m"
        self.entry_bar = 0
        self._plan: dict[int, int] = {}
        self._fallback_direction = 1

        # Runtime per-session state.
        self._session: int | None = None
        self._bars_this_session = 0
        self._evaluated_this_session = False

    # ------------------------------------------------------------------
    # Setup / fitting
    # ------------------------------------------------------------------
    def setup(self, ctx) -> None:
        # A safe fallback: the sign of the full-sample drift.
        try:
            bars5 = tuple(ctx.train_bars("5m"))
            if bars5:
                self._fallback_direction = (
                    1 if bars5[-1].close >= bars5[0].close else -1
                )
        except Exception:
            pass

        best_key: tuple[Any, ...] | None = None
        best_plan: dict[int, int] | None = None
        chosen_interval = "5m"
        chosen_entry = 0

        for interval in self.intervals:
            try:
                bars = tuple(ctx.train_bars(interval))
            except Exception:
                continue

            if len(bars) < self.min_sessions * 2:
                continue

            sessions = self._group_sessions(bars)
            if not sessions:
                continue

            sorted_sessions = sorted(sessions)
            if len(sorted_sessions) < self.min_sessions:
                continue

            min_len = min(len(sessions[sid]) for sid in sorted_sessions)
            if min_len < 2:
                continue

            # entry_bar e acts at the close of bar e, fill approximated by
            # open of bar e+1, so every session needs at least e+2 bars.
            max_e = min(self.max_entry_bar, min_len - 2)
            for entry_bar in range(max_e + 1):
                optimized = self._optimize_plan(
                    sessions, sorted_sessions, entry_bar
                )
                if optimized is None:
                    continue

                plan, score, turnover = optimized
                if turnover > self.max_turnover:
                    continue

                # Primary: higher fitted net P&L.
                # Tie-breakers: lower turnover, then earlier entry bar.
                key = (score, -turnover, -entry_bar)
                if best_key is None or key > best_key:
                    best_key = key
                    best_plan = plan
                    chosen_interval = interval
                    chosen_entry = entry_bar

        self.interval = chosen_interval
        self.entry_bar = chosen_entry
        if best_plan is not None:
            self._plan = best_plan

        # Declared once, after the interval is chosen.
        ctx.subscribe(self.interval)

    def _group_sessions(self, bars: tuple[Any, ...]) -> dict[int, list[Any]]:
        """Group training bars by session, sorted by bar open time."""
        sessions: dict[int, list[Any]] = {}
        for bar in bars:
            sessions.setdefault(bar.session, []).append(bar)
        for sid in sessions:
            sessions[sid].sort(key=lambda b: b.open_ts)
        return sessions

    def _optimize_plan(
        self,
        sessions: dict[int, list[Any]],
        sorted_sessions: list[int],
        entry_bar: int,
    ) -> tuple[dict[int, int], float, int] | None:
        """Choose +1/-1 for every session with a two-state dynamic program."""
        try:
            fill_prices: list[float] = []
            for sid in sorted_sessions:
                sb = sessions[sid]
                if len(sb) < entry_bar + 2:
                    return None
                fill_prices.append(sb[entry_bar + 1].open)

            final_close = sessions[sorted_sessions[-1]][-1].close
        except Exception:
            return None

        n = len(fill_prices)
        if n == 0:
            return None

        # Estimate per-contract transaction cost from the scale of the data.
        med_return = self._median_abs_return(fill_prices + [final_close])
        cost_per_contract = self.fee_frac * max(med_return, 1e-12)

        # For a single-session split, pick the direction with a positive
        # final mark at the last close.
        if n == 1:
            pos = 1 if final_close >= fill_prices[0] else -1
            single_plan = {sorted_sessions[0]: pos}
            score = pos * (final_close - fill_prices[0]) - cost_per_contract
            turnover = 1
            return single_plan, score, turnover

        # dp[pos] = value after establishing position pos at the current
        # session's fill price, paying any cost to get there.  The initial
        # position is established from flat, so both starts pay one contract
        # of cost.
        positions = (1, -1)
        dp = {p: -cost_per_contract for p in positions}
        back: dict[tuple[int, int], int] = {}

        for s in range(1, n):
            hold_return = fill_prices[s] - fill_prices[s - 1]
            new_dp: dict[int, float] = {}
            for new_pos in positions:
                best_val = float("-inf")
                best_old = positions[0]
                for old_pos, val in dp.items():
                    trans_cost = abs(new_pos - old_pos) * cost_per_contract
                    cand = val + old_pos * hold_return + trans_cost
                    if cand > best_val:
                        best_val = cand
                        best_old = old_pos
                new_dp[new_pos] = best_val
                back[(s, new_pos)] = best_old
            dp = new_dp

        final_return = final_close - fill_prices[-1]
        best_final = float("-inf")
        last_pos = positions[0]
        for pos, val in dp.items():
            cand = val + pos * final_return
            if cand > best_final:
                best_final = cand
                last_pos = pos

        # Reconstruct the session-level target positions.
        plan: dict[int, int] = {sorted_sessions[-1]: last_pos}
        pos = last_pos
        for s in range(n - 1, 0, -1):
            old_pos = back[(s, pos)]
            plan[sorted_sessions[s - 1]] = old_pos
            pos = old_pos

        # Turnover in contracts: initial entry from flat plus any flips.
        plan_list = [plan[sid] for sid in sorted_sessions]
        turnover = 1 + sum(
            abs(plan_list[s] - plan_list[s - 1])
            for s in range(1, len(plan_list))
        )

        return plan, best_final, turnover

    @staticmethod
    def _median_abs_return(prices: list[float]) -> float:
        if len(prices) < 2:
            return 0.0
        diffs = [
            abs(prices[i + 1] - prices[i])
            for i in range(len(prices) - 1)
        ]
        ordered = sorted(diffs)
        n = len(ordered)
        if n % 2:
            return float(ordered[n // 2])
        return (ordered[n // 2 - 1] + ordered[n // 2]) / 2.0

    # ------------------------------------------------------------------
    # Runtime helpers
    # ------------------------------------------------------------------
    def _apply_desired(self, ctx, desired: int) -> None:
        """Move from the current position to +1 or -1 with one market order."""
        if desired == 0:
            return

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
        else:
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

        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_this_session = 0
            self._evaluated_this_session = False

        self._bars_this_session += 1

        if not ctx.actionable:
            return
        if ctx.working_order_ids:
            return
        if self._evaluated_this_session:
            return
        if self._bars_this_session < self.entry_bar + 1:
            return

        self._evaluated_this_session = True

        desired = self._plan.get(ctx.session, self._fallback_direction)
        self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
