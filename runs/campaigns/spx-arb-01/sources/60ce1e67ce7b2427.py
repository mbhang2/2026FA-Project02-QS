"""A static options-surface arbitrage detector with generalized convexity.

Carries over the two relationships that made trial #1 the best so far --
vertical spreads cannot cost less than nothing or sell for more than their
width, and an equally spaced butterfly cannot have a negative price -- with
the same conservative execution: nearest few expiries only, one package at a
time, ask-for-buy/bid-for-sell, quoted-size checks, and a 0.50 point hurdle
after commission.

The new piece is the general butterfly inequality.  The question explicitly
asks about "three strikes in a row" being mispriced; the prior equal-spaced
butterfly only covers the case where the two wing gaps happen to be equal.
A call/put price surface must be convex in strike for *any* three strikes, so
we scan consecutive triples and, when the strike gaps are integer, use the
smallest integer weights that make the payoff non-negative:

    buy  (gap_right / gcd) contracts at the low strike
    sell (gap_left + gap_right) / gcd contracts at the middle strike
    buy  (gap_left / gcd) contracts at the high strike

The payoff is then non-negative for both calls and puts, and a negative cost
after commission is a locked edge.  Position size is capped to keep any one
convexity package from dominating the book.
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
        max_gen_body_size: int = 20,
    ):
        self.interval = interval
        self.min_edge = min_edge
        self.max_wing_step = max_wing_step
        self.max_expiries = max_expiries
        self.max_orders_per_session = max_orders_per_session
        self.max_gen_body_size = max_gen_body_size

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
        # Carry only one options package at a time.  Otherwise the book fills
        # with many overlapping legs, some of which may not settle inside the
        # scored window.
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

                # Two-leg vertical spreads.
                for step in range(1, max_step + 1):
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
                            long_vert_assets = (
                                self._asset_name(long_buy),
                                self._asset_name(long_sell),
                            )
                            long_vert_legs = (
                                ("buy", long_vert_assets[0], 1),
                                ("sell", long_vert_assets[1], 1),
                            )
                            consider(profit, long_vert_legs)

                        # Short vertical: receive more than the maximum possible
                        # future loss.
                        if short_sell.bid_size >= 1 and short_buy.ask_size >= 1:
                            credit = short_sell.bid - short_buy.ask
                            profit = credit - width - self._vert_comm
                            short_vert_assets = (
                                self._asset_name(short_sell),
                                self._asset_name(short_buy),
                            )
                            short_vert_legs = (
                                ("sell", short_vert_assets[0], 1),
                                ("buy", short_vert_assets[1], 1),
                            )
                            consider(profit, short_vert_legs)

                # Equally spaced butterfly: buy the wings, sell the body.
                for step in range(1, max_step + 1):
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
                        bfly_assets = (
                            self._asset_name(q1),
                            self._asset_name(q2),
                            self._asset_name(q3),
                        )
                        bfly_legs = (
                            ("buy", bfly_assets[0], 1),
                            ("sell", bfly_assets[1], 2),
                            ("buy", bfly_assets[2], 1),
                        )
                        consider(profit, bfly_legs)

                # Generalized convexity butterfly for consecutive strikes.
                # This is the "three strikes in a row" bound, not just the
                # equal-spaced special case.
                for j in range(1, n - 1):
                    q1 = arr[j - 1]
                    q2 = arr[j]
                    q3 = arr[j + 1]

                    d1 = q2.strike - q1.strike
                    d2 = q3.strike - q2.strike
                    if d1 <= 0.0 or d2 <= 0.0:
                        continue

                    d1_int = int(round(d1))
                    d2_int = int(round(d2))
                    if d1_int <= 0 or d2_int <= 0:
                        continue
                    if not math.isclose(d1, d1_int, rel_tol=1e-6, abs_tol=1e-9):
                        continue
                    if not math.isclose(d2, d2_int, rel_tol=1e-6, abs_tol=1e-9):
                        continue

                    g = math.gcd(d1_int, d2_int)
                    if g <= 0:
                        continue

                    # Smallest integer weights giving a non-negative payoff.
                    w_left = d2_int // g      # buy the low strike
                    w_right = d1_int // g     # buy the high strike
                    w_mid = w_left + w_right  # sell the middle strike

                    if w_mid <= 0 or w_mid > self.max_gen_body_size:
                        continue
                    if q1.ask_size < w_left or q3.ask_size < w_right:
                        continue
                    if q2.bid_size < w_mid:
                        continue

                    # Entry commission plus settlement commission is
                    # 0.1 * 2 * (w_left + w_mid + w_right)
                    # = 0.2 * 2 * w_mid = 0.4 * w_mid.
                    raw_credit = w_mid * q2.bid - w_left * q1.ask - w_right * q3.ask
                    profit = raw_credit - 0.4 * w_mid

                    gen_assets = (
                        self._asset_name(q1),
                        self._asset_name(q2),
                        self._asset_name(q3),
                    )
                    gen_legs = (
                        ("buy", gen_assets[0], w_left),
                        ("sell", gen_assets[1], w_mid),
                        ("buy", gen_assets[2], w_right),
                    )
                    consider(profit, gen_legs)

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
