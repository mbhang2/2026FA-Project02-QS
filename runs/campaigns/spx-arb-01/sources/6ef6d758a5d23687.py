"""A carry-free options-surface arbitrage detector with tighter execution.

This is deliberately close to the previous proposal, which already used the
right static-arbitrage relationships for a surface with unknown carry:
vertical spreads cannot cost less than nothing or sell for more than their
width, and an equally spaced butterfly cannot have a negative price.  Those
relationships hold whatever the riskless rate and dividend yield are.

The previous trial traded 642 times and was slightly negative.  The likely
causes are overtrading marginal edges and holding too many far-dated contracts
that never settle inside the scored window.  This version makes three
changes:

1. Only the nearest few expiries are considered.  Positions settle sooner,
   which reduces open mark-to-market at the end of the run.
2. Only one position is carried at a time (`ctx.assets_held()` stops further
   entries).  That prevents the book from filling with many overlapping
   long-dated packages.
3. The minimum locked edge after commission is raised from 0.05 to 0.50 index
   points.  That skips quote quantisation noise and small violations that do
   not survive the next-tick execution and the two-sided commission cost.

Every leg is priced against the side we would actually trade -- ask for buys,
bid for sells -- and checked for quoted size.  If nothing clears the higher
hurdle, the strategy reports nothing.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        min_edge: float = 0.5,
        max_wing_step: int = 4,
        max_expiries: int = 3,
        max_orders_per_session: int = 12,
    ):
        self.interval = interval
        self.min_edge = min_edge
        self.max_wing_step = max_wing_step
        self.max_expiries = max_expiries
        self.max_orders_per_session = max_orders_per_session

        self._tag = "vol-arb"
        self._vert_comm = 0.4   # 2 legs * 2 fills * 0.1
        self._bfly_comm = 0.8   # 4 legs * 2 fills * 0.1

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _asset_name(self, q) -> str:
        s = q.strike
        if abs(s - round(s)) < 1e-9:
            s_text = str(int(round(s)))
        else:
            s_text = f"{s:.10g}"
        side = "C" if q.is_call else "P"
        return f"SPX-{int(q.expiry_session)}-{side}-{s_text}"

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        if not ctx.actionable:
            return
        # Carry only one option position at a time.  Otherwise the book fills
        # with many packages, some of which may not settle before the run ends.
        if ctx.assets_held():
            return
        if ctx.working_order_ids:
            return

        quotes = ctx.options()
        if not quotes:
            return

        active_expiries = sorted(
            {int(q.expiry_session) for q in quotes if int(q.expiry_session) > ctx.session}
        )
        if not active_expiries:
            return
        allowed_expiries = set(active_expiries[: self.max_expiries])

        calls: Dict[int, List[Any]] = {}
        puts: Dict[int, List[Any]] = {}

        for q in quotes:
            exp = int(q.expiry_session)
            if exp <= ctx.session or exp not in allowed_expiries:
                continue
            if q.bid <= 0.0 or q.ask <= 0.0:
                continue
            bucket = calls if q.is_call else puts
            bucket.setdefault(exp, []).append(q)

        best: Optional[Tuple[float, Tuple[Tuple[str, str, int], ...]]] = None

        def consider(profit: float, legs: Tuple[Tuple[str, str, int], ...]) -> None:
            nonlocal best
            if profit <= self.min_edge:
                return
            if best is None or profit > best[0]:
                best = (profit, legs)

        for bucket_set in (calls, puts):
            for arr in bucket_set.values():
                arr.sort(key=lambda x: x.strike)
                n = len(arr)
                if n < 2:
                    continue

                max_step = min(self.max_wing_step, n - 1)

                for step in range(1, max_step + 1):
                    # Two-leg vertical spreads.
                    for i in range(0, n - step):
                        j = i + step
                        low = arr[i]
                        high = arr[j]
                        width = high.strike - low.strike
                        if width <= 0.0:
                            continue

                        if low.is_call:
                            long_buy, long_sell = low, high
                            short_sell, short_buy = low, high
                        else:
                            long_buy, long_sell = high, low
                            short_sell, short_buy = high, low

                        # Long vertical: buy the valuable leg, sell the cheap leg.
                        if long_buy.ask_size >= 1 and long_sell.bid_size >= 1:
                            cost = long_buy.ask - long_sell.bid
                            profit = -cost - self._vert_comm
                            assets: Tuple[str, ...] = (
                                self._asset_name(long_buy),
                                self._asset_name(long_sell),
                            )
                            legs: Tuple[Tuple[str, str, int], ...] = (
                                ("buy", assets[0], 1),
                                ("sell", assets[1], 1),
                            )
                            consider(profit, legs)

                        # Short vertical: receive more than the maximum possible
                        # future loss.
                        if short_sell.bid_size >= 1 and short_buy.ask_size >= 1:
                            credit = short_sell.bid - short_buy.ask
                            profit = credit - width - self._vert_comm
                            assets = (
                                self._asset_name(short_sell),
                                self._asset_name(short_buy),
                            )
                            legs = (
                                ("sell", assets[0], 1),
                                ("buy", assets[1], 1),
                            )
                            consider(profit, legs)

                    # Equally spaced butterfly: buy the wings, sell the body.
                    for j in range(step, n - step):
                        q1 = arr[j - step]
                        q2 = arr[j]
                        q3 = arr[j + step]
                        d1 = q2.strike - q1.strike
                        d2 = q3.strike - q2.strike
                        if d1 <= 0.0 or d2 <= 0.0:
                            continue
                        if not math.isclose(d1, d2, rel_tol=1e-6, abs_tol=1e-9):
                            continue
                        if q1.ask_size < 1 or q3.ask_size < 1 or q2.bid_size < 2:
                            continue

                        credit = 2.0 * q2.bid - q1.ask - q3.ask
                        profit = credit - self._bfly_comm
                        assets = (
                            self._asset_name(q1),
                            self._asset_name(q2),
                            self._asset_name(q3),
                        )
                        legs = (
                            ("buy", assets[0], 1),
                            ("sell", assets[1], 2),
                            ("buy", assets[2], 1),
                        )
                        consider(profit, legs)

        if best is None:
            return

        _, legs = best
        if len(legs) > self.max_orders_per_session:
            return

        for side, asset, size in legs:
            if side == "buy":
                oid = ctx.buy(size=size, asset=asset, tag=self._tag)
            else:
                oid = ctx.sell(size=size, asset=asset, tag=self._tag)
            if oid is None:
                return

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
