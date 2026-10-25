"""Trial #42: keep #36's proven unit-profit-first ranking, wing treatment and
inventory limits, but raise the box package size cap from 40 to 100.

#36 reached +5.7445 by doubling the box cap from 20 to 40 while keeping every
other #33 choice intact.  That is the cleanest positive evidence in the record:
when the chosen highest-unit-edge box has quoted touch depth above 20, more
contracts of the same deterministic terminal payoff are linearly additive after
commission.  Nothing in #36 says depth stops at 40.

This run tests the same linear scaling one step further.  For a long box the
terminal payoff is exactly K2 - K1, and the unit edge is net of the four legs'
entry and settlement commissions, so if any selected box has min(touched
sizes) above 40 the strategy now sizes into it instead of leaving the quoted
depth unused.  If depth never exceeds 40, this is exactly #36 and the score
will be identical.

I deliberately do not touch the wing package size: #40 raised it from 5 to 40
under the same ranking and slipped from +5.7445 to +5.6824.  I also keep the
wings enabled because #41 disabling them slipped to +5.5661.  The inventory
cap stays at 12 assets and one package per session; raising *asset* capacity was
confounded in #32 by the old total-first ranking and is not part of this clean
box-scaling experiment.
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
        max_box_step: int = 200,
        max_expiries: int = 10,
        max_wing_package_size: int = 5,
        max_box_package_size: int = 100,
        min_box_unit_edge: float = 0.05,
        max_assets: int = 12,
        max_packages_per_session: int = 1,
        max_orders_per_session: int = 12,
    ):
        self.interval = interval
        self.min_edge = min_edge
        self.max_wing_step = max_wing_step
        self.max_box_step = max_box_step
        self.max_expiries = max_expiries
        self.max_wing_package_size = max_wing_package_size
        self.max_box_package_size = max_box_package_size
        self.min_box_unit_edge = min_box_unit_edge
        self.max_assets = max_assets
        self.max_packages_per_session = max_packages_per_session
        self.max_orders_per_session = max_orders_per_session

        self._tag = "box-arb-unit-ranked-xlargecap-v1"
        self._vert_comm_per_unit = 0.4   # 2 legs * (entry + settlement) * 0.1
        self._bfly_comm_per_unit = 0.8   # 4 contracts * 2 fills * 0.1
        self._box_comm_per_unit = 0.8    # 4 legs * 2 fills * 0.1

        self._session: Optional[int] = None
        self._entries_this_session = 0

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

        if self._session != ctx.session:
            self._session = ctx.session
            self._entries_this_session = 0

        if self._entries_this_session >= self.max_packages_per_session:
            return
        if ctx.working_order_ids:
            return

        held_assets = set(ctx.assets_held())
        if len(held_assets) >= self.max_assets:
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
            if unit_profit <= 0.0 or units < 1:
                return

            total_profit = unit_profit * units
            if total_profit <= self.min_edge:
                return

            new_assets = {asset for _, asset, _ in base_legs}
            if any(asset in held_assets for asset in new_assets):
                return
            if len(held_assets) + len(new_assets) > self.max_assets:
                return

            duration = max(1, exp - ctx.session)
            # #33/#36's unit-profit-first key, preserved exactly.
            key = (unit_profit, total_profit, -duration)
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

                        # Long vertical: negative cost locks a non-negative
                        # terminal payoff.
                        avail_lv = int(min(long_buy.ask_size, long_sell.bid_size))
                        units_lv = min(avail_lv, self.max_wing_package_size)
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
                        units_sv = min(avail_sv, self.max_wing_package_size)
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
                    # A negative cost locks a non-negative terminal payoff.
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
                        units_bf = min(avail_bf, self.max_wing_package_size)
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

        # ----- Box spreads: terminal-payoff long boxes and extreme short boxes -----
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

                    # Long box: terminal payoff is exactly width.
                    avail_lb = int(
                        min(c1.ask_size, p1.bid_size, c2.bid_size, p2.ask_size)
                    )
                    units_lb = min(avail_lb, self.max_box_package_size)
                    if units_lb >= 1:
                        debit = c1.ask - p1.bid - c2.bid + p2.ask
                        unit_profit = width - debit - self._box_comm_per_unit
                        if unit_profit < self.min_box_unit_edge:
                            continue
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
                    # future loss.
                    avail_sb = int(
                        min(c1.bid_size, p1.ask_size, c2.ask_size, p2.bid_size)
                    )
                    units_sb = min(avail_sb, self.max_box_package_size)
                    if units_sb >= 1:
                        credit = c1.bid - p1.ask - c2.ask + p2.bid
                        unit_profit = credit - width - self._box_comm_per_unit
                        if unit_profit < self.min_box_unit_edge:
                            continue
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

        self._entries_this_session += 1

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
