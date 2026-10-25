"""A static-arbitrage options detector: verticals, butterflies, boxes, short butterflies, and fair-value boxes.

This keeps the exact mechanics that made trial #6 the best so far -- one option
package at a time, only the nearest few expiries, ask-for-buy / bid-for-sell,
quoted-size checks, and a 0.50 point hurdle after commission.

Changes versus #6:

1. **Short butterflies are added.** For three equal-spaced strikes, a short
   butterfly (sell the wings, buy two of the body) receives a credit and can
   lose at most the wing span. If the credit exceeds the maximum possible
   payout by at least the hurdle, it is a clean static violation. This is the
   mirror of the existing long-butterfly check.

2. **Positive-debit long boxes are no longer categorically ignored.** Instead of
   requiring a long box debit to be non-positive, we now infer a discount factor
   per expiry from the cross-section of box mid-values, and also take a long box
   when its executable debit is at least `min_edge` below the fair value implied
   by that discount factor. The old non-positive-debit case is retained.

The strategy is still deliberately conservative: if the inferred discount factor
is unavailable for an expiry, only the old carry-free box condition is used.
Positions are held to expiration where the locked payoff is paid.
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

    def _estimate_discounts(
        self,
        call_by_exp_strike: Dict[int, Dict[int, Any]],
        put_by_exp_strike: Dict[int, Dict[int, Any]],
    ) -> Dict[int, Optional[float]]:
        """Infer a single discount factor per expiry from adjacent box mids.

        The estimation only uses quotes with two-sided size, because those are
        the quotes a trade could actually be built from.
        """
        out: Dict[int, Optional[float]] = {}
        for exp, c_dict in call_by_exp_strike.items():
            p_dict = put_by_exp_strike.get(exp)
            if not p_dict:
                continue

            strikes = sorted(set(c_dict.keys()) & set(p_dict.keys()))
            if len(strikes) < 2:
                continue

            ratios: List[float] = []
            for a, b in zip(strikes, strikes[1:]):
                width = b - a
                if width <= 0.0:
                    continue

                c1 = c_dict[a]
                c2 = c_dict[b]
                p1 = p_dict[a]
                p2 = p_dict[b]

                if min(c1.ask_size, c1.bid_size, p1.ask_size, p1.bid_size,
                       c2.ask_size, c2.bid_size, p2.ask_size, p2.bid_size) < 1:
                    continue

                c1_mid = (c1.bid + c1.ask) / 2.0
                p1_mid = (p1.bid + p1.ask) / 2.0
                c2_mid = (c2.bid + c2.ask) / 2.0
                p2_mid = (p2.bid + p2.ask) / 2.0

                box_mid = c1_mid - p1_mid - c2_mid + p2_mid
                if box_mid <= 0.0:
                    continue

                r = box_mid / width
                # A discount factor for the nearest expiries should be near 1;
                # allow a generous band and let the median reject outliers.
                if 0.0 < r < 1.5:
                    ratios.append(r)

            if ratios:
                ratios.sort()
                out[exp] = ratios[len(ratios) // 2]
            else:
                out[exp] = None

        return out

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        if not ctx.actionable:
            return
        # Carry only one options package at a time.  The previous best trial
        # showed that this focus is worth more than adding overlapping packages.
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

        call_lists: Dict[int, List[Any]] = {}
        put_lists: Dict[int, List[Any]] = {}
        call_by_exp_strike: Dict[int, Dict[int, Any]] = {}
        put_by_exp_strike: Dict[int, Dict[int, Any]] = {}

        for q in quotes:
            exp = int(q.expiry_session)
            if exp <= ctx.session or exp not in allowed_expiries:
                continue
            if q.bid <= 0.0 or q.ask <= 0.0:
                continue
            if q.bid_size < 1 or q.ask_size < 1:
                continue

            if q.is_call:
                call_lists.setdefault(exp, []).append(q)
                call_by_exp_strike.setdefault(exp, {})[q.strike] = q
            else:
                put_lists.setdefault(exp, []).append(q)
                put_by_exp_strike.setdefault(exp, {})[q.strike] = q

        discount_by_exp = self._estimate_discounts(
            call_by_exp_strike, put_by_exp_strike
        )

        best: Optional[Tuple[float, Tuple[Tuple[str, str, int], ...]]] = None

        def consider(profit: float, candidate_legs: Tuple[Tuple[str, str, int], ...]) -> None:
            nonlocal best
            if profit <= self.min_edge:
                return
            if best is None or profit > best[0]:
                best = (profit, candidate_legs)

        # ----- Two-leg vertical spreads and equal-spaced butterflies -----
        for bucket_set in (call_lists, put_lists):
            for arr in bucket_set.values():
                arr.sort(key=lambda x: x.strike)
                n = len(arr)
                if n < 2:
                    continue

                max_step = min(self.max_wing_step, n - 1)

                for step in range(1, max_step + 1):
                    # Long and short vertical spreads.
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
                        # A negative cost locks a non-negative payoff.
                        if long_buy.ask_size >= 1 and long_sell.bid_size >= 1:
                            cost = long_buy.ask - long_sell.bid
                            profit = -cost - self._vert_comm
                            lv_assets = (
                                self._asset_name(long_buy),
                                self._asset_name(long_sell),
                            )
                            lv_legs = (
                                ("buy", lv_assets[0], 1),
                                ("sell", lv_assets[1], 1),
                            )
                            consider(profit, lv_legs)

                        # Short vertical: receive more than the maximum future
                        # loss of the spread.
                        if short_sell.bid_size >= 1 and short_buy.ask_size >= 1:
                            credit = short_sell.bid - short_buy.ask
                            profit = credit - width - self._vert_comm
                            sv_assets = (
                                self._asset_name(short_sell),
                                self._asset_name(short_buy),
                            )
                            sv_legs = (
                                ("sell", sv_assets[0], 1),
                                ("buy", sv_assets[1], 1),
                            )
                            consider(profit, sv_legs)

                    # Equal-spaced butterfly: buy the wings, sell the body.
                    # A negative cost locks a non-negative payoff.
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

                        # Long butterfly: buy q1, sell 2*q2, buy q3.
                        if q1.ask_size < 1 or q3.ask_size < 1 or q2.bid_size < 2:
                            continue

                        credit = 2.0 * q2.bid - q1.ask - q3.ask
                        profit = credit - self._bfly_comm
                        bf_assets = (
                            self._asset_name(q1),
                            self._asset_name(q2),
                            self._asset_name(q3),
                        )
                        bf_legs = (
                            ("buy", bf_assets[0], 1),
                            ("sell", bf_assets[1], 2),
                            ("buy", bf_assets[2], 1),
                        )
                        consider(profit, bf_legs)

                        # Short butterfly: sell q1, buy 2*q2, sell q3.
                        # The maximum possible payout to the counterparty is d1.
                        if q1.bid_size < 1 or q3.bid_size < 1 or q2.ask_size < 2:
                            continue

                        credit = q1.bid + q3.bid - 2.0 * q2.ask
                        profit = credit - d1 - self._bfly_comm
                        short_bf_legs = (
                            ("sell", bf_assets[0], 1),
                            ("buy", bf_assets[1], 2),
                            ("sell", bf_assets[2], 1),
                        )
                        consider(profit, short_bf_legs)

        # ----- Box spreads: calls and puts at two strikes, same expiry -----
        for exp in allowed_expiries:
            c_dict = call_by_exp_strike.get(exp)
            p_dict = put_by_exp_strike.get(exp)
            if not c_dict or not p_dict:
                continue

            d_est = discount_by_exp.get(exp)

            strikes = sorted(set(c_dict.keys()) & set(p_dict.keys()))
            if len(strikes) < 2:
                continue

            max_step = min(self.max_wing_step, len(strikes) - 1)

            for step in range(1, max_step + 1):
                for i in range(0, len(strikes) - step):
                    k1 = strikes[i]
                    k2 = strikes[i + step]
                    width = k2 - k1
                    if width <= 0.0:
                        continue

                    c1 = c_dict[k1]
                    c2 = c_dict[k2]
                    p1 = p_dict[k1]
                    p2 = p_dict[k2]

                    # Long box: buy low-strike call, sell low-strike put,
                    # sell high-strike call, buy high-strike put.  The terminal
                    # payoff is exactly width.
                    #
                    # We take it in two cases:
                    # 1. debit <= 0: pure violation with no dependence on carry.
                    # 2. debit is at least min_edge below the fair box value
                    #    implied by this expiry's cross-section discount factor.
                    if (
                        c1.ask_size >= 1
                        and p1.bid_size >= 1
                        and c2.bid_size >= 1
                        and p2.ask_size >= 1
                    ):
                        debit = c1.ask - p1.bid - c2.bid + p2.ask
                        long_case = debit <= 0.0
                        if d_est is not None and d_est > 0.0:
                            fair = d_est * width
                            if debit < fair - self.min_edge:
                                long_case = True

                        if long_case:
                            profit = width - debit - self._box_comm
                            lb_assets = (
                                self._asset_name(c1),
                                self._asset_name(p1),
                                self._asset_name(c2),
                                self._asset_name(p2),
                            )
                            box_lb_legs = (
                                ("buy", lb_assets[0], 1),
                                ("sell", lb_assets[1], 1),
                                ("sell", lb_assets[2], 1),
                                ("buy", lb_assets[3], 1),
                            )
                            consider(profit, box_lb_legs)

                    # Short box: sell low-strike call, buy low-strike put,
                    # buy high-strike call, sell high-strike put.  The maximum
                    # future loss is exactly width, so a credit above width is a
                    # pure violation.
                    if (
                        c1.bid_size >= 1
                        and p1.ask_size >= 1
                        and c2.ask_size >= 1
                        and p2.bid_size >= 1
                    ):
                        credit = c1.bid - p1.ask - c2.ask + p2.bid
                        if credit >= width:
                            profit = credit - width - self._box_comm
                            sb_assets = (
                                self._asset_name(c1),
                                self._asset_name(p1),
                                self._asset_name(c2),
                                self._asset_name(p2),
                            )
                            box_sb_legs = (
                                ("sell", sb_assets[0], 1),
                                ("buy", sb_assets[1], 1),
                                ("buy", sb_assets[2], 1),
                                ("sell", sb_assets[3], 1),
                            )
                            consider(profit, box_sb_legs)

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
