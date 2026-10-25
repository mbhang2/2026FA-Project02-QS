"""A density-ranked carry-aware arbitrage scan with a relative box-spread layer.

Trial #12 remains the best single candidate so far: carry-free verticals,
equal-spaced long butterflies, and individual terminal-payoff box spreads, one
package at a time, held to settlement.  #18 added size scaling and
expiry-density ranking without changing the score.

This version keeps #18 exactly and adds one new, genuinely relative object:

    **Relative box spread.**  Two boxes of the same width and the same expiry
    both have the same terminal payoff width.  If one box can be sold at a
    bid higher than the price at which another box of equal width can be
    bought, the terminal payoffs cancel exactly and the difference in initial
    cash is riskless.  This detects relative mispricing between different
    strike pairs, not just the level of a single box's discount -- which may
    simply be carry.

The individual box logic is unchanged.  The relative box is implemented by
grouping intervals by strike width per expiry and pairing the cheapest box to
buy (lowest ask-side cost) with the dearest box to sell (highest bid-side
credit).  Those are then passed through the same density/size ranking as every
other candidate, so a relative box is only chosen when it is better per
session held.
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
        max_box_step: int = 40,
        max_rel_box_step: int = 80,
        max_expiries: int = 6,
        max_package_size: int = 10,
        max_orders_per_session: int = 12,
    ):
        self.interval = interval
        self.min_edge = min_edge
        self.max_wing_step = max_wing_step
        self.max_box_step = max_box_step
        self.max_rel_box_step = max_rel_box_step
        self.max_expiries = max_expiries
        self.max_package_size = max_package_size
        self.max_orders_per_session = max_orders_per_session

        self._tag = "arb-box-relative"
        self._vert_comm_per_unit = 0.4   # 2 legs * (entry + settlement) * 0.1
        self._bfly_comm_per_unit = 0.8   # 4 contracts * 2 fills * 0.1
        self._box_comm_per_unit = 0.8    # 4 legs * 2 fills * 0.1
        self._rel_box_comm_per_unit = 1.6  # 8 legs * 2 fills * 0.1

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
        # One options package at a time.  The best prior configuration shows
        # this focus is worth more than overlapping packages.
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

        allowed_expiries = active_expiries[: self.max_expiries]
        allowed_set = set(allowed_expiries)

        call_lists: Dict[int, List[Any]] = {}
        put_lists: Dict[int, List[Any]] = {}
        call_by_exp_strike: Dict[int, Dict[int, Any]] = {}
        put_by_exp_strike: Dict[int, Dict[int, Any]] = {}

        for q in quotes:
            exp = int(q.expiry_session)
            if exp <= ctx.session or exp not in allowed_set:
                continue
            if q.bid <= 0.0 or q.ask <= 0.0:
                continue

            if q.is_call:
                call_lists.setdefault(exp, []).append(q)
                call_by_exp_strike.setdefault(exp, {})[q.strike] = q
            else:
                put_lists.setdefault(exp, []).append(q)
                put_by_exp_strike.setdefault(exp, {})[q.strike] = q

        best_key: Optional[Tuple[float, float, int]] = None
        best_legs: Optional[Tuple[Tuple[str, str, int], ...]] = None

        def consider(
            unit_profit: float,
            units: int,
            exp: int,
            base_legs: Tuple[Tuple[str, str, int], ...],
        ) -> None:
            nonlocal best_key, best_legs
            if unit_profit <= self.min_edge or units < 1:
                return

            duration = max(1, exp - ctx.session)
            total_profit = unit_profit * units
            density = total_profit / duration

            # Highest profit per session held, then highest total profit, then
            # shortest time to settlement.
            key = (density, total_profit, -duration)
            if best_key is None or key > best_key:
                best_key = key
                best_legs = tuple(
                    (side, asset, int(size * units))
                    for side, asset, size in base_legs
                )

        # ----- Carry-free vertical spreads and equal-spaced long butterflies -----
        for bucket_set in (call_lists, put_lists):
            for exp in allowed_expiries:
                arr = bucket_set.get(exp)
                if not arr:
                    continue

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

                        # Long vertical: buy the valuable leg, sell the cheap
                        # leg.  A negative cost locks a non-negative payoff.
                        avail_lv = int(min(long_buy.ask_size, long_sell.bid_size))
                        units_lv = min(avail_lv, self.max_package_size)
                        if units_lv >= 1:
                            cost = long_buy.ask - long_sell.bid
                            unit_profit = -cost - self._vert_comm_per_unit
                            lv_assets = (
                                self._asset_name(long_buy),
                                self._asset_name(long_sell),
                            )
                            lv_base = (
                                ("buy", lv_assets[0], 1),
                                ("sell", lv_assets[1], 1),
                            )
                            consider(unit_profit, units_lv, exp, lv_base)

                        # Short vertical: receive more than the maximum future
                        # loss of the spread.
                        avail_sv = int(min(short_sell.bid_size, short_buy.ask_size))
                        units_sv = min(avail_sv, self.max_package_size)
                        if units_sv >= 1:
                            credit = short_sell.bid - short_buy.ask
                            unit_profit = credit - width - self._vert_comm_per_unit
                            sv_assets = (
                                self._asset_name(short_sell),
                                self._asset_name(short_buy),
                            )
                            sv_base = (
                                ("sell", sv_assets[0], 1),
                                ("buy", sv_assets[1], 1),
                            )
                            consider(unit_profit, units_sv, exp, sv_base)

                    # Equal-spaced long butterfly: buy the wings, sell the body.
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

                        avail_bf = int(min(q1.ask_size, q3.ask_size, q2.bid_size // 2))
                        units_bf = min(avail_bf, self.max_package_size)
                        if units_bf < 1:
                            continue

                        credit = 2.0 * q2.bid - q1.ask - q3.ask
                        unit_profit = credit - self._bfly_comm_per_unit
                        bf_assets = (
                            self._asset_name(q1),
                            self._asset_name(q2),
                            self._asset_name(q3),
                        )
                        bf_base = (
                            ("buy", bf_assets[0], 1),
                            ("sell", bf_assets[1], 2),
                            ("buy", bf_assets[2], 1),
                        )
                        consider(unit_profit, units_bf, exp, bf_base)

        # ----- Individual box spreads: terminal-payoff long boxes and extreme
        # short boxes.  Kept exactly from #18.
        for exp in allowed_expiries:
            c_dict = call_by_exp_strike.get(exp)
            p_dict = put_by_exp_strike.get(exp)
            if not c_dict or not p_dict:
                continue

            strikes = sorted(set(c_dict.keys()) & set(p_dict.keys()))
            if len(strikes) < 2:
                continue

            max_step = min(self.max_box_step, len(strikes) - 1)

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

                    # Long box: terminal payoff is exactly width.  The simulator
                    # charges no financing cost, so any debit below width is a
                    # guaranteed terminal profit before commissions; settlement
                    # converts it into realized P&L.
                    avail_lb = int(min(c1.ask_size, p1.bid_size, c2.bid_size, p2.ask_size))
                    units_lb = min(avail_lb, self.max_package_size)
                    if units_lb >= 1:
                        debit = c1.ask - p1.bid - c2.bid + p2.ask
                        unit_profit = width - debit - self._box_comm_per_unit
                        lb_assets = (
                            self._asset_name(c1),
                            self._asset_name(p1),
                            self._asset_name(c2),
                            self._asset_name(p2),
                        )
                        lb_base = (
                            ("buy", lb_assets[0], 1),
                            ("sell", lb_assets[1], 1),
                            ("sell", lb_assets[2], 1),
                            ("buy", lb_assets[3], 1),
                        )
                        consider(unit_profit, units_lb, exp, lb_base)

                    # Short box: credit above width fully pays the maximum
                    # future loss, independent of carry.
                    avail_sb = int(min(c1.bid_size, p1.ask_size, c2.ask_size, p2.bid_size))
                    units_sb = min(avail_sb, self.max_package_size)
                    if units_sb >= 1:
                        credit = c1.bid - p1.ask - c2.ask + p2.bid
                        unit_profit = credit - width - self._box_comm_per_unit
                        sb_assets = (
                            self._asset_name(c1),
                            self._asset_name(p1),
                            self._asset_name(c2),
                            self._asset_name(p2),
                        )
                        sb_base = (
                            ("sell", sb_assets[0], 1),
                            ("buy", sb_assets[1], 1),
                            ("buy", sb_assets[2], 1),
                            ("sell", sb_assets[3], 1),
                        )
                        consider(unit_profit, units_sb, exp, sb_base)

        # ----- Relative box spreads: equal terminal payoff, opposite initial
        # cash.  Two boxes of exactly the same width and expiry are the same
        # terminal asset.  If the offer of one is below the bid of another,
        # buying the cheap one and selling the dear one locks the difference.
        for exp in allowed_expiries:
            c_dict = call_by_exp_strike.get(exp)
            p_dict = put_by_exp_strike.get(exp)
            if not c_dict or not p_dict:
                continue

            strikes = sorted(set(c_dict.keys()) & set(p_dict.keys()))
            if len(strikes) < 2:
                continue

            max_step = min(self.max_rel_box_step, len(strikes) - 1)

            # Group intervals by width.  Each item carries the interval's
            # ask-side cost (what it costs to buy the box) and bid-side credit
            # (what is received to sell the box).
            groups: Dict[float, List[Dict[str, Any]]] = {}

            for step in range(1, max_step + 1):
                for i in range(0, len(strikes) - step):
                    j = i + step
                    k1 = strikes[i]
                    k2 = strikes[j]
                    width = k2 - k1
                    if width <= 0.0:
                        continue

                    c1 = c_dict[k1]
                    c2 = c_dict[k2]
                    p1 = p_dict[k1]
                    p2 = p_dict[k2]

                    ask_debit = c1.ask - p1.bid - c2.bid + p2.ask
                    bid_credit = c1.bid - p1.ask - c2.ask + p2.bid

                    avail_ask = int(min(c1.ask_size, p1.bid_size, c2.bid_size, p2.ask_size))
                    avail_bid = int(min(c1.bid_size, p1.ask_size, c2.ask_size, p2.bid_size))

                    if avail_ask < 1 and avail_bid < 1:
                        continue

                    width_key = round(width, 6)
                    groups.setdefault(width_key, []).append(
                        {
                            "ask_debit": ask_debit,
                            "bid_credit": bid_credit,
                            "avail_ask": avail_ask,
                            "avail_bid": avail_bid,
                            "k1": k1,
                            "k2": k2,
                            "c1": c1,
                            "c2": c2,
                            "p1": p1,
                            "p2": p2,
                        }
                    )

            for entries in groups.values():
                min_ask_entry: Optional[Dict[str, Any]] = None
                max_bid_entry: Optional[Dict[str, Any]] = None

                for entry in entries:
                    if entry["avail_ask"] >= 1:
                        if (
                            min_ask_entry is None
                            or entry["ask_debit"] < min_ask_entry["ask_debit"]
                        ):
                            min_ask_entry = entry

                    if entry["avail_bid"] >= 1:
                        if (
                            max_bid_entry is None
                            or entry["bid_credit"] > max_bid_entry["bid_credit"]
                        ):
                            max_bid_entry = entry

                if min_ask_entry is None or max_bid_entry is None:
                    continue

                unit_profit = (
                    max_bid_entry["bid_credit"]
                    - min_ask_entry["ask_debit"]
                    - self._rel_box_comm_per_unit
                )
                if unit_profit <= self.min_edge:
                    continue

                units = min(
                    min_ask_entry["avail_ask"],
                    max_bid_entry["avail_bid"],
                    self.max_package_size,
                )
                if units < 1:
                    continue

                # Long box: buy low call, sell low put, sell high call, buy
                # high put.
                lo = min_ask_entry
                lo_c1 = lo["c1"]
                lo_c2 = lo["c2"]
                lo_p1 = lo["p1"]
                lo_p2 = lo["p2"]

                # Short box: sell low call, buy low put, buy high call, sell
                # high put.
                sh = max_bid_entry
                sh_c1 = sh["c1"]
                sh_c2 = sh["c2"]
                sh_p1 = sh["p1"]
                sh_p2 = sh["p2"]

                rel_base = (
                    ("buy", self._asset_name(lo_c1), 1),
                    ("sell", self._asset_name(lo_p1), 1),
                    ("sell", self._asset_name(lo_c2), 1),
                    ("buy", self._asset_name(lo_p2), 1),
                    ("sell", self._asset_name(sh_c1), 1),
                    ("buy", self._asset_name(sh_p1), 1),
                    ("buy", self._asset_name(sh_c2), 1),
                    ("sell", self._asset_name(sh_p2), 1),
                )
                consider(unit_profit, units, exp, rel_base)

        if best_legs is None:
            return
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
