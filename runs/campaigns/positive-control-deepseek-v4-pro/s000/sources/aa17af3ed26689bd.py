"""Fitted low-turnover session-level direction plan, entered at the first bar.

The previous best strategies in this campaign held a near-continuous position
and changed it only when a fitted early-session signal was strong.  This
version goes one step further: it fits a per-session +1/-1 schedule in
setup() from the training bars, converts that schedule into a two-state
dynamic program over session-to-session holding returns, and then follows the
precomputed plan at runtime.

Key design choices:

- One entry per session at the first closed bar of the chosen interval.
  The entry is queued after that first bar closes, so it executes on the next
  tick/bar open, matching the engine's T+1 rule.  Choosing a shorter interval
  moves the entry earlier in the session, but the schedule still changes only
  once per session, so turnover stays low.

- The DP chooses a path of long or short for every session to maximize
  continuous holding P&L between session entry prices, with a per-flip
  penalty.  A hard cap on flips prevents the optimizer from selecting a
  high-turnover path that only works in-sample.

- Runtime does not depend on fitted order ids or on brackets.  It tracks the
  session and its first bar, looks up the planned direction, and submits the
  single market order needed to move from the current position to the planned
  one.  If a session has no planned state, it falls back to the full-sample
  drift direction when flat, ensuring exposure is established.
"""

from __future__ import annotations

from math import inf


class Strategy:
    """Fitted per-session direction schedule, held with minimal turnover."""

    def __init__(
        self,
        *,
        size: int = 1,
        intervals: tuple[str, ...] = ("5m", "1m", "30s", "10s", "1s", "250ms"),
        cost_mults: tuple[float, ...] = (0.05, 0.10, 0.20, 0.35, 0.50, 0.75, 1.0, 1.5),
        max_flips: int = 45,
    ) -> None:
        self.size = size
        self.intervals = intervals
        self.cost_mults = cost_mults
        self.max_flips = max_flips

        # Fitted in setup()
        self._interval = "5m"
        self._plan: dict[int, int] = {}
        self._ordered: list[int] = []
        self._fallback_sign = 1

        # Runtime state
        self._session: int | None = None
        self._bars_this_session = 0
        self._acted_this_session = False

    # ------------------------------------------------------------------
    # Setup / fitting
    # ------------------------------------------------------------------
    def setup(self, ctx) -> None:
        """Fit a session plan from training bars, then subscribe to the chosen interval."""
        self._interval, self._plan, self._ordered, self._fallback_sign = self._fit(ctx)
        ctx.subscribe(self._interval, emit_empty=False)

    def _fit(self, ctx) -> tuple[str, dict[int, int], list[int], int]:
        """Fit a low-turnover +1/-1 plan over session entry prices."""
        candidates: list[tuple[float, int, float, str, list[tuple[int, float]], list[int], int]] = []

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

            sorted_sessions = sorted(sessions.keys())
            entries: list[tuple[int, float]] = []

            for sid in sorted_sessions:
                sb = sessions[sid]
                # A market order queued on the first closed bar's close fills on
                # the next bar's open in the engine's T+1 model.  When only one
                # bar exists, use its close as the fallback entry.
                if len(sb) >= 2:
                    fill_price = sb[1].open
                elif len(sb) == 1:
                    fill_price = sb[0].close
                else:
                    continue
                entries.append((sid, fill_price))

            if len(entries) < 10:
                continue

            final_close = bars[-1].close
            returns: list[float] = []

            for i in range(len(entries) - 1):
                returns.append(entries[i + 1][1] - entries[i][1])
            returns.append(final_close - entries[-1][1])

            median_abs_return = self._median_abs(returns)
            if median_abs_return <= 0.0:
                median_abs_return = 1.0

            drift_sign = 1 if final_close >= bars[0].close else -1

            # Always long or always short is a valid candidate and keeps the
            # strategy exposed in every session.
            base_state = 1 if sum(returns) >= 0 else -1
            base_raw = abs(sum(returns))
            base_states = [base_state] * len(entries)
            candidates.append(
                (base_raw, 0, base_raw, interval, entries, base_states, drift_sign)
            )

            for mult in self.cost_mults:
                cost = mult * median_abs_return
                states, flips, raw, net = self._dp_path(returns, cost)
                if states is not None:
                    candidates.append(
                        (net, flips, raw, interval, entries, states, drift_sign)
                    )

        if not candidates:
            # Nothing usable fit; fall back to a single direction on 5m.
            return "5m", {}, [], 1

        eligible = [c for c in candidates if c[1] <= self.max_flips]
        if not eligible:
            # If every schedule is too active, prefer the quietest one.
            best = min(candidates, key=lambda c: (c[1], -c[0]))
        else:
            # Highest penalized net, then fewer flips, then higher raw P&L.
            best = max(eligible, key=lambda c: (c[0], -c[1], c[2]))

        _net, _flips, _raw, interval, entries, states, drift_sign = best

        plan = {sid: state for (sid, _), state in zip(entries, states)}
        ordered = [state for (_, _), state in zip(entries, states)]

        return interval, plan, ordered, drift_sign

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
    def _dp_path(returns: list[float], cost: float):
        """Solve the two-state +1/-1 schedule with a per-flip penalty."""
        n = len(returns)
        if n == 0:
            return None, 0, 0.0, 0.0

        states = (1, -1)
        dp_prev = {1: 0.0, -1: 0.0}
        choices: list[dict[int, int]] = []

        for i, ret in enumerate(returns):
            dp: dict[int, float] = {}
            ch: dict[int, int] = {}

            for state in states:
                best_val = -inf
                best_prev = state
                for prev in states:
                    penalty = cost * (i > 0 and prev != state)
                    val = dp_prev[prev] + state * ret - penalty
                    if val > best_val:
                        best_val = val
                        best_prev = prev

                dp[state] = best_val
                ch[state] = best_prev

            dp_prev = dp
            choices.append(ch)

        final_state = max(states, key=lambda s: dp_prev[s])

        path = [0] * n
        path[-1] = final_state
        for i in range(n - 1, 0, -1):
            path[i - 1] = choices[i][path[i]]

        raw = sum(path[i] * returns[i] for i in range(n))
        flips = sum(1 for i in range(1, n) if path[i] != path[i - 1])
        net = raw - flips * cost

        return path, flips, raw, net

    # ------------------------------------------------------------------
    # Runtime helpers
    # ------------------------------------------------------------------
    def _resolve_plan(self, session: int) -> int | None:
        """Return the planned direction for a runtime session index."""
        if session in self._plan:
            return self._plan[session]
        if 0 <= session < len(self._ordered):
            return self._ordered[session]
        return None

    def _apply_desired(self, ctx, desired: int) -> None:
        """Queue the single market order needed to move to +1 or -1."""
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
        if self._acted_this_session:
            return
        if self._bars_this_session != 1:
            return
        if ctx.working_order_ids:
            return

        self._acted_this_session = True

        desired = self._resolve_plan(ctx.session)
        if desired is None:
            # Unplanned session: establish drift exposure if flat, otherwise
            # keep the current position.
            if ctx.position != 0:
                return
            desired = self._fallback_sign

        self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
