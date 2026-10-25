"""Carry-free options arbitrage with upper butterflies and scaled sizes.

Builds on the best trial (#1), which kept only the carry-free static bounds:
vertical spreads cannot cost less than zero or sell for more than their width,
and an equally spaced butterfly cannot have a negative price.  This version
adds two things that should not hurt and can help:

1. The opposite butterfly bound: an equally spaced butterfly cannot cost more
   than its maximum payoff.  Selling the wings and buying the body locks in a
   profit when that bound is executable at quoted size.  The previous
   strategies only scanned the "middle too dear" side.

2. Quote sizes are used, so a candidate with several contracts behind the
   touch can be entered with size up to a modest cap.  The number of assets
   held is capped separately, so we still do not build the large unfocused
   book that made trial #0 negative.

All candidate legs are priced against the side we would actually trade (buy at
ask, sell at bid) and are checked for quoted size.  If nothing clears the
hurdle after commission, no order is placed.
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
        max_assets_held: int = 5,
        max_size: int = 2,
        max_orders_per_session: int = 12,
    ):
        self.interval = interval
        self.min_edge = min_edge
        self.max_wing_step = max_wing_step
        self.max_expiries = max_expiries
        self.max_assets_held = max_assets_held
        self.max_size = max_size
        self.max_orders_per_session = max_orders_per_session

        self._tag = "vol-arb"
        # Per size-1 package, entry plus settlement commission:
        # vertical: 2 contracts * 2 fills * 0.1 = 0.4
        # butterfly: 4 contracts * 2 fills * 0.1 = 0.8
        self._vert_comm = 0.4
        self._bfly_comm = 0.8

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
        if ctx.working_order_ids:
            return

        held_assets = set(ctx.assets_held())
        if len(held_assets) >= self.max_assets_held:
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

            unique = {asset for _, asset, _ in legs}
            if any(asset in held_assets for asset in unique):
                return
            if len(held_assets) + len(unique) > self.max_assets_held:
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
                    # Two-leg vertical spreads, long and short bounds.
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

                        # Long vertical: buy valuable leg, sell cheap leg.
                        if long_buy.ask_size >= 1 and long_sell.bid_size >= 1:
                            size = min(
                                self.max_size,
                                long_buy.ask_size,
                                long_sell.bid_size,
                            )
                            if size >= 1:
                                cost_per = long_buy.ask - long_sell.bid
                                net_per = -cost_per - self._vert_comm
                                profit = size * net_per
                                long_assets = (
                                    self._asset_name(long_buy),
                                    self._asset_name(long_sell),
                                )
                                long_legs = (
                                    ("buy", long_assets[0], size),
                                    ("sell", long_assets[1], size),
                                )
                                consider(profit, long_legs)

                        # Short vertical: receive more than maximum future loss.
                        if short_sell.bid_size >= 1 and short_buy.ask_size >= 1:
                            size = min(
                                self.max_size,
                                short_sell.bid_size,
                                short_buy.ask_size,
                            )
                            if size >= 1:
                                credit_per = short_sell.bid - short_buy.ask
                                net_per = credit_per - width - self._vert_comm
                                profit = size * net_per
                                short_assets = (
                                    self._asset_name(short_sell),
                                    self._asset_name(short_buy),
                                )
                                short_legs = (
                                    ("sell", short_assets[0], size),
                                    ("buy", short_assets[1], size),
                                )
                                consider(profit, short_legs)

                    # Equally spaced butterfly: two static bounds.
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
                        d = float(d1)

                        # Lower bound: a long butterfly cannot have negative cost.
                        if q1.ask_size >= 1 and q3.ask_size >= 1 and q2.bid_size >= 2:
                            size = min(
                                self.max_size,
                                q1.ask_size,
                                q3.ask_size,
                                q2.bid_size // 2,
                            )
                            if size >= 1:
                                net_per = 2.0 * q2.bid - q1.ask - q3.ask - self._bfly_comm
                                profit = size * net_per
                                bfly_assets = (
                                    self._asset_name(q1),
                                    self._asset_name(q2),
                                    self._asset_name(q3),
                                )
                                bfly_legs = (
                                    ("buy", bfly_assets[0], size),
                                    ("sell", bfly_assets[1], 2 * size),
                                    ("buy", bfly_assets[2], size),
                                )
                                consider(profit, bfly_legs)

                        # Upper bound: a long butterfly cannot cost more than d,
                        # so if selling wings and buying the body gives more than
                        # d after commission, a short butterfly locks a profit.
                        if q1.bid_size >= 1 and q3.bid_size >= 1 and q2.ask_size >= 2:
                            size = min(
                                self.max_size,
                                q1.bid_size,
                                q3.bid_size,
                                q2.ask_size // 2,
                            )
                            if size >= 1:
                                credit_per = q1.bid + q3.bid - 2.0 * q2.ask
                                net_per = credit_per - d - self._bfly_comm
                                profit = size * net_per
                                short_bfly_assets = (
                                    self._asset_name(q1),
                                    self._asset_name(q2),
                                    self._asset_name(q3),
                                )
                                short_bfly_legs = (
                                    ("sell", short_bfly_assets[0], size),
                                    ("buy", short_bfly_assets[1], 2 * size),
                                    ("sell", short_bfly_assets[2], size),
                                )
                                consider(profit, short_bfly_legs)

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
