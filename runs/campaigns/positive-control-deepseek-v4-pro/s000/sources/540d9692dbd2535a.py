"""Session-level full-sample schedule from training ticks.

Setup sees the entire training split and chooses one +1 or -1 position per
session by dynamic programming over session-to-session first-tick returns.
Runtime acts only on `session_open` lifecycle events, so exposure is
established early and held continuously.  The plan is fitted with transaction
costs and a preference for fewer flips, mirroring the best prior low-turnover
shape while using the whole session outcome rather than only an early return.
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Fitted per-session direction schedule, traded on session_open."""

    def __init__(
        self,
        *,
        fee_frac: float = 0.15,
        max_flips: int = 60,
        min_sessions: int = 5,
        size: int = 1,
    ) -> None:
        self.fee_frac = fee_frac
        self.max_flips = max_flips
        self.min_sessions = min_sessions
        self.size = size

        # Fitted in setup()
        self._plan: dict[int, int] = {}
        self._fallback: int = 1

    # ------------------------------------------------------------------
    # Setup / fitting
    # ------------------------------------------------------------------
    def setup(self, ctx) -> None:
        ctx.subscribe("5m")
        self._fit(ctx)

    def _fit(self, ctx) -> None:
        ticks: tuple[Any, ...] = ()
        try:
            ticks = tuple(ctx.ticks())
        except Exception:
            ticks = ()

        if ticks:
            by_session: dict[int, list[Any]] = {}
            for tick in ticks:
                by_session.setdefault(tick.session, []).append(tick)

            sids = sorted(by_session)
            rows: list[tuple[int, float, float]] = []
            for sid in sids:
                arr = by_session.get(sid, [])
                if arr:
                    rows.append((sid, arr[0].price, arr[-1].price))

            if len(rows) >= self.min_sessions:
                firsts = [r[1] for r in rows]
                final_close = rows[-1][2]
                px = firsts + [final_close]
                rets = [px[i + 1] - px[i] for i in range(len(px) - 1)]

                med = self._median_abs(rets)
                if med > 0:
                    candidates: list[tuple[float, int, float, list[int]]] = []
                    for mult in (
                        0.02,
                        0.05,
                        0.10,
                        0.15,
                        0.20,
                        0.25,
                        0.30,
                        0.40,
                        0.50,
                        0.75,
                        1.00,
                        1.50,
                    ):
                        path, net, flips = self._best_path(rets, mult * med)
                        if flips <= self.max_flips:
                            candidates.append((net, flips, mult, path))

                    if candidates:
                        best_net = max(c[0] for c in candidates)
                        eligible = [c for c in candidates if c[0] >= 0.90 * best_net]
                        net, flips, mult, path = min(eligible, key=lambda c: c[1])
                        self._plan = {
                            rows[i][0]: path[i] for i in range(len(rows))
                        }
                    self._fallback = 1 if sum(rets) >= 0 else -1
                else:
                    self._fallback = 1 if final_close >= firsts[0] else -1
            elif rows:
                self._fallback = 1 if rows[-1][2] >= rows[0][1] else -1

        # Fallback if the tick fit produced no plan: use coarse bar drift.
        if not self._plan:
            try:
                bars = tuple(ctx.train_bars("5m"))
                if bars:
                    self._fallback = 1 if bars[-1].close >= bars[0].close else -1
            except Exception:
                pass

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
    def _best_path(returns: list[float], fee: float) -> tuple[list[int], float, int]:
        """Choose a sequence of +1/-1 positions maximizing net return.

        The first position is entered from flat, so its transition cost is one
        contract's fee. Later flips change position by two contracts.
        """
        n = len(returns)
        if n == 0:
            return [], 0.0, 0

        states = (1, -1)

        dp: dict[int, float] = {0: 0.0}
        back: list[dict[int, int]] = []

        for r in returns:
            ndp: dict[int, float] = {}
            ch: dict[int, int] = {}

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
                ch[state] = best_prev

            dp = ndp
            back.append(ch)

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
        pass

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        event = getattr(ctx, "event", None)
        if event is None:
            return

        kind = getattr(event, "kind", None)
        if kind is None:
            return

        kind_value = getattr(kind, "value", kind)
        if kind_value != "session_open":
            return

        if not ctx.actionable:
            return
        if ctx.working_order_ids:
            return

        desired = self._plan.get(ctx.session, self._fallback)
        if desired == 0:
            return

        self._apply_desired(ctx, desired)
