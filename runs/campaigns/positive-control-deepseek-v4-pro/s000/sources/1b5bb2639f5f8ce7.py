from __future__ import annotations


class Strategy:
    """Fitted session-transition schedule from the training split.

    setup() reads the training split as bars, groups them by session, and
    determines the price at which a market order queued on the first bar's close
    would fill (the next bar's open).  It then solves a small dynamic program
    over the known sequence of session fill prices to choose, for each session,
    whether to be long, short, or flat.  The transition cost is scaled by the
    average absolute inter-session move, which keeps turnover modest.  At
    runtime the first bar of each session triggers the precomputed plan.

    This is intentionally low-frequency: one decision per session, with market
    orders sized to move between flat, +1, and -1 positions.
    """

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 1,
        trade_cost_frac: float = 0.02,
    ) -> None:
        self.interval = interval
        self.size = size
        self.trade_cost_frac = trade_cost_frac

        self._plan: dict[int, int] = {}
        self._fallback = False
        self._fallback_direction = 0

        self._session: int | None = None
        self._bars_this_session = 0
        self._decided = False

    # ------------------------------------------------------------------
    # Setup / plan fitting
    # ------------------------------------------------------------------
    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        self._fit_plan(ctx)

    def _fit_plan(self, ctx) -> None:
        """Fit a per-session position schedule from training bars."""
        try:
            bars = tuple(ctx.train_bars(self.interval))
        except Exception:
            bars = ()

        if not bars:
            self._fallback = True
            self._fallback_direction = 0
            return

        sessions: dict[int, list] = {}
        for bar in bars:
            sessions.setdefault(bar.session, []).append(bar)

        sids = sorted(sessions.keys())
        entries: list[tuple[int, float]] = []

        for sid in sids:
            sb = sessions[sid]
            # Need a next bar for a T+1 market fill after the first bar close.
            if len(sb) >= 2:
                entries.append((sid, sb[1].open))

        if not entries:
            self._fallback = True
            self._fallback_direction = 0
            return

        session_ids = [sid for sid, _ in entries]
        entry_prices = [price for _, price in entries]
        final_close = sessions[sids[-1]][-1].close

        # Return from one session's fill to the next session's fill.  The last
        # leg is marked at the final close of the training split.
        n = len(entries)
        rets = []
        for i in range(n):
            next_price = entry_prices[i + 1] if i + 1 < n else final_close
            rets.append(next_price - entry_prices[i])

        avg_abs_ret = sum(abs(r) for r in rets) / max(1, n)
        trade_cost = self.trade_cost_frac * avg_abs_ret

        positions = (-1, 0, 1)

        # Backward dynamic programming over future session fill prices.
        value = {p: 0.0 for p in positions}
        policy: dict[tuple[int, int], int] = {}

        for i in range(n - 1, -1, -1):
            ret = rets[i]
            new_value = {}
            new_policy = {}

            for prev in positions:
                best_value = -1e100
                best_p = 0

                for p in positions:
                    cost = trade_cost * abs(p - prev)
                    candidate = -cost + p * ret + value[p]

                    if candidate > best_value:
                        best_value = candidate
                        best_p = p

                new_value[prev] = best_value
                new_policy[(i, prev)] = best_p

            value = new_value
            policy.update(new_policy)

        # Reconstruct the chosen position for each session.
        self._plan = {}
        prev = 0
        for i in range(n):
            p = policy[(i, prev)]
            self._plan[session_ids[i]] = p
            prev = p

        # If the DP chose flat everywhere, fall back to the overall drift so
        # the strategy is still exposed rather than idle.
        if not any(self._plan.values()):
            drift = final_close - entry_prices[0]
            if drift > 0:
                self._fallback_direction = 1
            elif drift < 0:
                self._fallback_direction = -1
            else:
                self._fallback_direction = 0
            self._fallback = True

    # ------------------------------------------------------------------
    # Runtime helpers
    # ------------------------------------------------------------------
    def _apply_desired(self, ctx, desired: int) -> None:
        """Queue the market order needed to move from current pos to desired."""
        pos = ctx.position

        if desired > 0:
            if pos == 0:
                ctx.buy(size=self.size, tif="DAY", tag="plan-long")
            elif pos < 0:
                ctx.buy(
                    size=-pos + self.size,
                    tif="DAY",
                    tag="plan-flip-long",
                )
        elif desired < 0:
            if pos == 0:
                ctx.sell(size=self.size, tif="DAY", tag="plan-short")
            elif pos > 0:
                ctx.sell(
                    size=pos + self.size,
                    tif="DAY",
                    tag="plan-flip-short",
                )
        else:
            if pos > 0:
                ctx.sell(size=pos, tif="DAY", tag="plan-close-long")
            elif pos < 0:
                ctx.buy(size=-pos, tif="DAY", tag="plan-close-short")

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------
    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return

        # New session: reset the per-session gate.
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_this_session = 0
            self._decided = False

        self._bars_this_session += 1

        if not ctx.actionable:
            return
        if ctx.working_order_ids:
            return
        if self._decided:
            return

        # The plan decides on the first bar close, with fill on the next bar's
        # open, matching the fitting model.
        if self._bars_this_session != 1:
            return

        if self._fallback:
            if self._fallback_direction != 0:
                self._apply_desired(ctx, self._fallback_direction)
            else:
                # Basic last-bar direction fallback if no schedule was fit.
                history = ctx.history(self.interval)
                if len(history) < 1:
                    return
                last = history[-1]
                if last.close > last.open:
                    self._apply_desired(ctx, 1)
                elif last.close < last.open:
                    self._apply_desired(ctx, -1)
                else:
                    self._decided = True
                    return
        else:
            desired = self._plan.get(ctx.session, 0)
            self._apply_desired(ctx, desired)

        self._decided = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
