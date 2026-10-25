"""A static options-surface arbitrage detector, extended beyond separate calls/puts.

The previous best trial used only within-side relationships:
  - vertical spreads cannot cost less than nothing or sell for more than width
  - equally spaced butterflies cannot have a negative price

Those are carried over unchanged.  This version adds two more relationships
that are also independent of unknown carry/dividend assumptions:

1. Box spreads.  A long box has a fixed, positive payoff of (K2-K1) at
   expiry, so its quoted cost must not be negative.  A short box has a fixed,
   negative payoff of (K2-K1), so its quoted credit must not exceed (K2-K1).
   Violations are pure options-surface mispricings and do not need the
   underlying forward.

2. The opposite butterfly bound.  The prior trial only checked the lower
   bound (a long butterfly cannot be entered for negative cash).  A short
   butterfly has max loss equal to the wing step, so its quoted credit must
   not exceed that step.

Execution remains deliberately selective: only the nearest few expiries are
scanned, only one package is carried at a time, every leg is priced against
the side we would actually trade, and quoted size is checked before an edge
is considered.  If nothing clears the hurdle, the strategy places no orders.
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
        max_wing_step: int = 6,
        max_box_step: int = 4,
        max_expiries: int = 3,
        max_orders_per_session: int = 12,
    ):
        self.interval = interval
        self.min_edge = min_edge
        self.max_wing_step = max_wing_step
        self.max_box_step = max_box_step
        self.max_expiries = max_expiries
        self.max_orders_per_session = max_orders_per_session

        self._tag = "vol-arb"
        self._vert_comm = 0.4   # 2 legs * 2 fills * 0.1
        self._bfly_comm = 0.8   # 4 legs * 2 fills * 0.1
        self._box_comm = 0.8    # 4 legs * 2 fills * 0.1

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
        # with many overlapping packages, some of which may not settle before
        # the run ends.
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

        def consider(profit: float, candidate_legs: Tuple[Tuple[str, str, int], ...]) -> None:
            nonlocal best
            if profit <= self.min_edge:
                return
            if best is None or profit > best[0]:
                best = (profit, candidate_legs)

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
                            vert_long_assets = (
                                self._asset_name(long_buy),
                                self._asset_name(long_sell),
                            )
                            vert_long_legs = (
                                ("buy", vert_long_assets[0], 1),
                                ("sell", vert_long_assets[1], 1),
                            )
                            consider(profit, vert_long_legs)

                        # Short vertical: receive more than the maximum possible
                        # future loss.
                        if short_sell.bid_size >= 1 and short_buy.ask_size >= 1:
                            credit = short_sell.bid - short_buy.ask
                            profit = credit - width - self._vert_comm
                            vert_short_assets = (
                                self._asset_name(short_sell),
                                self._asset_name(short_buy),
                            )
                            vert_short_legs = (
                                ("sell", vert_short_assets[0], 1),
                                ("buy", vert_short_assets[1], 1),
                            )
                            consider(profit, vert_short_legs)

                    # Lower butterfly bound: a long butterfly (buy the wings,
                    # sell the body) cannot have negative cash.
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
                        long_bfly_assets = (
                            self._asset_name(q1),
                            self._asset_name(q2),
                            self._asset_name(q3),
                        )
                        long_bfly_legs = (
                            ("buy", long_bfly_assets[0], 1),
                            ("sell", long_bfly_assets[1], 2),
                            ("buy", long_bfly_assets[2], 1),
                        )
                        consider(profit, long_bfly_legs)

                    # Upper butterfly bound: a short butterfly (sell the wings,
                    # buy the body) has max loss equal to the wing step.
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
                        if q1.bid_size < 1 or q3.bid_size < 1 or q2.ask_size < 2:
                            continue

                        credit = q1.bid + q3.bid - 2.0 * q2.ask
                        profit = credit - d1 - self._bfly_comm
                        short_bfly_assets = (
                            self._asset_name(q1),
                            self._asset_name(q2),
                            self._asset_name(q3),
                        )
                        short_bfly_legs = (
                            ("sell", short_bfly_assets[0], 1),
                            ("buy", short_bfly_assets[1], 2),
                            ("sell", short_bfly_assets[2], 1),
                        )
                        consider(profit, short_bfly_legs)

        # Box spreads: calls and puts at two strikes form a fixed payoff of
        # (K2-K1), so the same static bound logic applies across the two sides.
        for exp, arr in calls.items():
            put_arr = puts.get(exp)
            if not put_arr:
                continue

            call_by_strike = {q.strike: q for q in arr}
            put_by_strike = {q.strike: q for q in put_arr}
            strikes = sorted(set(call_by_strike) & set(put_by_strike))
            n_strikes = len(strikes)
            if n_strikes < 2:
                continue

            max_step = min(self.max_box_step, n_strikes - 1)

            for step in range(1, max_step + 1):
                for i in range(0, n_strikes - step):
                    j = i + step
                    k1 = strikes[i]
                    k2 = strikes[j]
                    width = k2 - k1
                    if width <= 0.0:
                        continue

                    c1 = call_by_strike[k1]
                    p1 = put_by_strike[k1]
                    c2 = call_by_strike[k2]
                    p2 = put_by_strike[k2]

                    # Long box: buy c1, sell p1, sell c2, buy p2.
                    # Payoff is exactly width.  A negative cost is free money.
                    if (
                        c1.ask_size >= 1
                        and p1.bid_size >= 1
                        and c2.bid_size >= 1
                        and p2.ask_size >= 1
                    ):
                        cost = c1.ask - p1.bid - c2.bid + p2.ask
                        profit = -cost - self._box_comm
                        long_box_assets = (
                            self._asset_name(c1),
                            self._asset_name(p1),
                            self._asset_name(c2),
                            self._asset_name(p2),
                        )
                        long_box_legs = (
                            ("buy", long_box_assets[0], 1),
                            ("sell", long_box_assets[1], 1),
                            ("sell", long_box_assets[2], 1),
                            ("buy", long_box_assets[3], 1),
                        )
                        consider(profit, long_box_legs)

                    # Short box: sell c1, buy p1, buy c2, sell p2.
                    # Max future loss is width, so credit cannot exceed width.
                    if (
                        c1.bid_size >= 1
                        and p1.ask_size >= 1
                        and c2.ask_size >= 1
                        and p2.bid_size >= 1
                    ):
                        credit = c1.bid - p1.ask - c2.ask + p2.bid
                        profit = credit - width - self._box_comm
                        short_box_assets = (
                            self._asset_name(c1),
                            self._asset_name(p1),
                            self._asset_name(c2),
                            self._asset_name(p2),
                        )
                        short_box_legs = (
                            ("sell", short_box_assets[0], 1),
                            ("buy", short_box_assets[1], 1),
                            ("buy", short_box_assets[2], 1),
                            ("sell", short_box_assets[3], 1),
                        )
                        consider(profit, short_box_legs)

        if best is None:
            return

        _, best_legs = best
        if len(best_legs) > self.max_orders_per_session:
            return

        for side, asset, size in best_legs:
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
