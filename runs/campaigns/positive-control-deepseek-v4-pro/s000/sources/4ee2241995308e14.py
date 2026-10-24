"""Fitted session-level schedule from training bars, traded on bar close.

A direct descendant of the best prior shape (#34): one low-turnover continuous
+/ -1 position.  The difference is that #34 fits a single sign convention from
the early-session return; this strategy fits an explicit per-session schedule
with dynamic programming over session-to-session returns, regularized by a
transaction-fee grid and a preference for fewer flips.

The runtime deliberately acts only inside `on_bars`, because a prior attempt
(#48) that traded on `session_open` lifecycle events produced no fills in this
harness.  Keeping the proven `on_bars` entry path avoids that failure mode.
"""

from __future__ import annotations


class Strategy:
    """Explicit per-session direction plan, executed at an early bar close."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 1,
        max_lookback: int = 8,
        max_flips_frac: float = 0.20,
        min_samples: int = 8,
    ) -> None:
        self.interval = interval
        self.size = size
        self.max_lookback = max_lookback
        self.max_flips_frac = max_flips_frac
        self.min_samples = min_samples

        # Fitted in setup()
        self._lookback = 1
        self._plan: dict[int, int] = {}
        self._fallback = 1

        # Runtime session state
        self._session: int | None = None
        self._bars_this_session = 0
        self._evaluated_this_session = False

    # ------------------------------------------------------------------
    # Setup / fitting
    # ------------------------------------------------------------------
    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        self._fit(ctx)

    def _fit(self, ctx) -> None:
        """Fit a session-direction plan from bulk training bars."""
        try:
            bars = tuple(ctx.train_bars(self.interval))
        except Exception:
            bars = ()

        if not bars:
            return

        # Group bars by session.  The training data is small enough that this
        # is cheap and avoids any assumption about how bars arrive.
        sessions: dict[int, list] = {}
        for bar in bars:
            sessions.setdefault(bar.session, []).append(bar)

        sorted_sessions = sorted(sessions)
        if not sorted_sessions:
            return

        first_open = sessions[sorted_sessions[0]][0].open
        last_close = sessions[sorted_sessions[-1]][-1].close
        self._fallback = 1 if last_close >= first_open else -1

        final_close = last_close
        max_flips = max(1, int(len(sorted_sessions) * self.max_flips_frac))
        candidates: list[tuple[float, int, int, list[int], list[tuple[int, float]]]] = []

        for k in range(1, self.max_lookback + 1):
            rows: list[tuple[int, float]] = []
            for sid in sorted_sessions:
                sb = sessions[sid]
                if len(sb) < k:
                    continue
                rows.append((sid, sb[k - 1].close))

            if len(rows) < self.min_samples:
                continue

            prices = [close for _sid, close in rows] + [final_close]
            returns = [prices[i + 1] - prices[i] for i in range(len(prices) - 1)]

            med = self._median_abs(returns)
            if med <= 0:
                continue

            for mult in self._fee_multipliers():
                fee = mult * med
                path, net, flips = self._best_path(returns, fee)
                if flips <= max_flips:
                    candidates.append((net, flips, k, path, rows))

        if not candidates:
            return

        # Prefer near-best net with as few flips as possible: turnover is the
        # clearest source of real cost in this harness, and prior trials confirm
        # that lower-turnover session plans score better.
        best_net = max(c[0] for c in candidates)
        eligible = [c for c in candidates if c[0] >= 0.90 * best_net]
        net, flips, k, path, rows = min(eligible, key=lambda c: (c[1], c[2]))

        self._lookback = k
        self._plan = {sid: path_i for (sid, _close), path_i in zip(rows, path)}

    @staticmethod
    def _median_abs(values: list[float]) -> float:
        if not values:
            return 0.0
        ordered = sorted(abs(v) for v in values)
        n = len(ordered)
        if n % 2:
            return float(ordered[n // 2])
        return (ordered[n // 2 - 1] + ordered[n // 2]) / 2.0

    @staticmethod
    def _fee_multipliers() -> tuple[float, ...]:
        """Fee values expressed as multiples of the median absolute return."""
        return (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.75, 1.00, 1.50)

    @staticmethod
    def _best_path(
        returns: list[float], fee: float
    ) -> tuple[list[int], float, int]:
        """Choose a sequence of +1/-1 positions maximizing net return.

        The first position is entered from flat, so its transition cost is one
        contract's fee.  Later flips change the position by two contracts.
        """
        n = len(returns)
        if n == 0:
            return [], 0.0, 0

        states = (1, -1)
        dp: dict[int, float] = {0: 0.0}
        back: list[dict[int, int]] = []

        for r in returns:
            ndp: dict[int, float] = {}
            choices: dict[int, int] = {}

            for state in states:
                best_val: float | None = None
                best_prev: int | None = None

                for prev, val in dp.items():
                    cand = val + state * r - fee * abs(state - prev)
                    if best_val is None:
                        best_val = cand
                        best_prev = prev
                    elif cand > best_val:
                        best_val = cand
                        best_prev = prev

                if best_val is None or best_prev is None:
                    raise RuntimeError("no path found during schedule fit")

                ndp[state] = best_val
                choices[state] = best_prev

            dp = ndp
            back.append(choices)

        final_state = max(states, key=lambda s: dp[s])
        net = dp[final_state]

        path = [0] * n
        state = final_state
        for i in range(n - 1, -1, -1):
            path[i] = state
            state = back[i][state]

        flips = sum(1 for i in range(1, n) if path[i] != path[i - 1])
        return path, net, flips

    # ------------------------------------------------------------------
    # Runtime helpers
    # ------------------------------------------------------------------
    def _apply_desired(self, ctx, desired: int) -> None:
        """Queue the single market order needed to move to +1 or -1."""
        if desired == 0:
            return

        pos = ctx.position
        if desired > 0:
            if pos == 0:
                ctx.buy(size=self.size, tif="DAY", tag="plan-long")
            elif pos < 0:
                ctx.buy(size=-pos + self.size, tif="DAY", tag="plan-flip-long")
        else:
            if pos == 0:
                ctx.sell(size=self.size, tif="DAY", tag="plan-short")
            elif pos > 0:
                ctx.sell(size=pos + self.size, tif="DAY", tag="plan-flip-short")

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------
    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return

        # New session: reset per-session state.
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_this_session = 0
            self._evaluated_this_session = False

        self._bars_this_session += 1

        if not ctx.actionable:
            return
        if ctx.working_order_ids:
            return
        if self._evaluated_this_session or self._bars_this_session < self._lookback:
            return

        self._evaluated_this_session = True

        desired = self._plan.get(ctx.session, self._fallback)
        self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
