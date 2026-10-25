"""A widened pure static-arbitrage options detector, built on trial #6.

Trial #6 is the best so far: one options package at a time, quoted-touch
execution, ask-for-buy / bid-for-sell, quoted-size checks, and carry-free
vertical, equal-spaced butterfly, and box-spread conditions.

This version keeps that engine and expands the search in two ways that do not
depend on the unknown carry:

1. **Wider and longer search.** We scan up to the nearest 8 active expiries and
   use strike-index steps up to 10 for verticals, equal-spaced butterflies, and
   box spreads.  Box and vertical inequalities are carry-free regardless of
   width or expiry, so widening the search only adds opportunities that already
   satisfy the static no-arbitrage bounds.

2. **Adjacent weighted butterflies for unequal strike spacing.** For three
   consecutive strikes with integral distances d12, d23 and weights
   a = d23 / gcd(d12,d23), b = d12 / gcd(d12,d23), c = a + b, a long weighted
   butterfly has non-negative terminal payoff and must not cost less than zero;
   a short weighted butterfly receives a credit and can lose at most a * d12.
   These are the standard convexity conditions, but only implemented for adjacent
   strikes and only when the total contract count is small enough to keep the
   package tradable at the quoted sizes.

Everything else from #6 is unchanged.  No positive-debit boxes are taken using
carry estimates, and no quote is discarded solely for having a zero bid or ask
size -- individual legs are checked for the specific side each order needs.
If the surface is clean, no orders are placed.
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
        max_wing_step: int = 10,
        max_expiries: int = 8,
        max_orders_per_session: int = 12,
        max_gen_bfly_contracts: int = 12,
    ):
        self.interval = interval
        self.min_edge = min_edge
        self.max_wing_step = max_wing_step
        self.max_expiries = max_expiries
        self.max_orders_per_session = max_orders_per_session
        self.max_gen_bfly_contracts = max_gen_bfly_contracts

        self._tag = "vol-arb"
        self._vert_comm = 0.4   # 2 legs * 2 fills * 0.1
        self._bfly_comm = 0.8   # 4 legs * 2 fills * 0.1
        self._box_comm = 0.8    # 4 legs * 2 fills * 0.1

    @staticmethod
    def _int_strike(x: float) -> Optional[int]:
        rounded = round(x)
        if abs(x - rounded) < 1e-9:
            return int(rounded)
        return None

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
        # Carry only one options package at a time.  The previous best trial
        # showed that this focus is worth more than overlapping packages.
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

            if q.is_call:
                call_lists.setdefault(exp, []).append(q)
                call_by_exp_strike.setdefault(exp, {})[q.strike] = q
            else:
                put_lists.setdefault(exp, []).append(q)
                put_by_exp_strike.setdefault(exp, {})[q.strike] = q

        best: Optional[Tuple[float, Tuple[Tuple[str, str, int], ...]]] = None

        def consider(profit: float, candidate_legs: Tuple[Tuple[str, str, int], ...]) -> None:
            nonlocal best
            if profit <= self.min_edge:
                return
            if best is None or profit > best[0]:
                best = (profit, candidate_legs)

        # ----- Two-leg vertical spreads, equal-spaced butterflies, and -----
        # ----- adjacent weighted butterflies (carry-free convexity). -----
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

                # Adjacent weighted butterflies for unequal strike spacing.
                # This is a pure convexity relation and uses exact integer
                # weights only when the three strikes are whole points.
                if n < 3:
                    continue

                for idx in range(n - 2):
                    q1 = arr[idx]
                    q2 = arr[idx + 1]
                    q3 = arr[idx + 2]

                    k1 = self._int_strike(q1.strike)
                    k2 = self._int_strike(q2.strike)
                    k3 = self._int_strike(q3.strike)
                    if k1 is None or k2 is None or k3 is None:
                        continue

                    d12 = k2 - k1
                    d23 = k3 - k2
                    if d12 <= 0 or d23 <= 0:
                        continue
                    # Equal spacing is already handled above.
                    if d12 == d23:
                        continue

                    g = math.gcd(d12, d23)
                    a = d23 // g
                    b = d12 // g
                    c = a + b
                    total_contracts = 2 * (a + b)

                    if total_contracts > self.max_gen_bfly_contracts:
                        continue

                    assets = (
                        self._asset_name(q1),
                        self._asset_name(q2),
                        self._asset_name(q3),
                    )

                    # Long weighted butterfly: buy the wings, sell the body.
                    # A negative cost locks a non-negative terminal payoff.
                    if (
                        q1.ask_size >= a
                        and q3.ask_size >= b
                        and q2.bid_size >= c
                    ):
                        credit = c * q2.bid - a * q1.ask - b * q3.ask
                        profit = credit - 0.2 * total_contracts
                        long_legs = (
                            ("buy", assets[0], a),
                            ("sell", assets[1], c),
                            ("buy", assets[2], b),
                        )
                        consider(profit, long_legs)

                    # Short weighted butterfly: sell the wings, buy the body.
                    # The maximum terminal loss is a * d12, so a larger credit
                    # is a pure static violation.
                    if (
                        q1.bid_size >= a
                        and q3.bid_size >= b
                        and q2.ask_size >= c
                    ):
                        credit = a * q1.bid + b * q3.bid - c * q2.ask
                        max_payout = a * d12
                        profit = credit - max_payout - 0.2 * total_contracts
                        short_legs = (
                            ("sell", assets[0], a),
                            ("buy", assets[1], c),
                            ("sell", assets[2], b),
                        )
                        consider(profit, short_legs)

        # ----- Box spreads: calls and puts at two strikes, same expiry -----
        for exp in allowed_expiries:
            c_dict = call_by_exp_strike.get(exp)
            p_dict = put_by_exp_strike.get(exp)
            if not c_dict or not p_dict:
                continue

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
                    # payoff is exactly width.  We trade only if debit <= 0:
                    # a zero or negative cost for a positive payoff is a pure
                    # violation and does not rely on knowing the carry.
                    if (
                        c1.ask_size >= 1
                        and p1.bid_size >= 1
                        and c2.bid_size >= 1
                        and p2.ask_size >= 1
                    ):
                        debit = c1.ask - p1.bid - c2.bid + p2.ask
                        if debit <= 0.0:
                            profit = width - debit - self._box_comm
                            lb_assets = (
                                self._asset_name(c1),
                                self._asset_name(p1),
                                self._asset_name(c2),
                                self._asset_name(p2),
                            )
                            lb_legs = (
                                ("buy", lb_assets[0], 1),
                                ("sell", lb_assets[1], 1),
                                ("sell", lb_assets[2], 1),
                                ("buy", lb_assets[3], 1),
                            )
                            consider(profit, lb_legs)

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
                            sb_legs = (
                                ("sell", sb_assets[0], 1),
                                ("buy", sb_assets[1], 1),
                                ("buy", sb_assets[2], 1),
                                ("sell", sb_assets[3], 1),
                            )
                            consider(profit, sb_legs)

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
