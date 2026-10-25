"""A scaled multi-package options arbitrage detector, generalising the best prior trial.

Trial #12 layered terminal-payoff long boxes on top of the carry-free static
arbitrage scan and reached +1.3393 with 51 packages.  It still left two things
on the table:

1. It traded one contract per leg even when the quoted touch had more size.
   Options market orders fill at the touch up to quoted size, so a candidate
   that clears the hurdle can be sized up to the minimum size required by all
   its legs without risking a partial fill.

2. It opened at most one package at a time and then waited for expiry.  If two
   disjoint arbitrage packages are available at the same instant, both are
   riskless on their own; taking only the best one leaves money behind.

This strategy keeps the full candidate set of #12 -- carry-free verticals,
equal-spaced long butterflies, and both long terminal-payoff boxes and extreme
short boxes -- and then:

- sizes each candidate by the largest whole number its legs' quoted sizes allow,
- ranks candidates by total post-commission profit rather than per-unit profit,
- greedily chooses several *disjoint* packages in a callback,
- continues opening new disjoint packages on later bars while holding earlier
  ones, filtering out any asset already held.

The detector is deliberately conservative: the static conditions are exactly
those of #12, the per-unit edge is still filtered by `min_edge`, and a session
order budget is kept well below the engine's 64-action cap so a package is never
left half-submitted.
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
        max_box_step: int = 80,
        max_expiries: int = 12,
        max_scale: int = 10,
        max_packages_per_session: int = 4,
        max_orders_per_session: int = 48,
    ):
        self.interval = interval
        self.min_edge = min_edge
        self.max_wing_step = max_wing_step
        self.max_box_step = max_box_step
        self.max_expiries = max_expiries
        self.max_scale = max_scale
        self.max_packages_per_session = max_packages_per_session
        self.max_orders_per_session = max_orders_per_session

        self._tag = "scaled-multi-arb"
        self._vert_comm = 0.4   # 2 legs * 2 fills * 0.1
        self._bfly_comm = 0.8   # 4 leg-contracts * 2 fills * 0.1
        self._box_comm = 0.8    # 4 leg-contracts * 2 fills * 0.1

        self._session: Optional[int] = None
        self._orders_this_session = 0

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

        if ctx.session != self._session:
            self._session = ctx.session
            self._orders_this_session = 0

        # Let earlier queued orders finish cleanly before opening more packages.
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

        held_assets = set(ctx.assets_held())
        candidates: List[Tuple[float, Tuple[str, ...], Tuple[Tuple[str, str, int], ...]]] = []

        def add_candidate(
            profit: float,
            scale: int,
            base_legs: Tuple[Tuple[str, str, int], ...],
        ) -> None:
            if profit <= self.min_edge or scale < 1:
                return
            leg_assets = tuple(asset for _, asset, _ in base_legs)
            # Skip candidates that would trade an asset already in an open
            # package.  Each package is riskless on its own; keeping them
            # disjoint means the combination is also riskless and clean to
            # settle at expiry.
            if any(asset in held_assets for asset in leg_assets):
                return
            scaled_legs = tuple(
                (side, asset, size * scale)
                for side, asset, size in base_legs
            )
            candidates.append((profit * scale, leg_assets, scaled_legs))

        # ----- Carry-free vertical spreads and equal-spaced butterflies -----
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

                        if long_buy.ask_size >= 1 and long_sell.bid_size >= 1:
                            cost = long_buy.ask - long_sell.bid
                            profit = -cost - self._vert_comm
                            scale = min(
                                int(long_buy.ask_size),
                                int(long_sell.bid_size),
                                self.max_scale,
                            )
                            lv_assets = (
                                self._asset_name(long_buy),
                                self._asset_name(long_sell),
                            )
                            lv_legs = (
                                ("buy", lv_assets[0], 1),
                                ("sell", lv_assets[1], 1),
                            )
                            add_candidate(profit, scale, lv_legs)

                        if short_sell.bid_size >= 1 and short_buy.ask_size >= 1:
                            credit = short_sell.bid - short_buy.ask
                            profit = credit - width - self._vert_comm
                            scale = min(
                                int(short_sell.bid_size),
                                int(short_buy.ask_size),
                                self.max_scale,
                            )
                            sv_assets = (
                                self._asset_name(short_sell),
                                self._asset_name(short_buy),
                            )
                            sv_legs = (
                                ("sell", sv_assets[0], 1),
                                ("buy", sv_assets[1], 1),
                            )
                            add_candidate(profit, scale, sv_legs)

                    # Equal-spaced long butterfly.
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
                        body_capacity = int(q2.bid_size) // 2
                        scale = min(
                            int(q1.ask_size),
                            int(q3.ask_size),
                            body_capacity,
                            self.max_scale,
                        )
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
                        add_candidate(profit, scale, bf_legs)

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

                    # Long box: terminal payoff is exactly width, and the
                    # simulator charges no financing cost, so any debit below
                    # width is collected at expiry.
                    if (
                        c1.ask_size >= 1
                        and p1.bid_size >= 1
                        and c2.bid_size >= 1
                        and p2.ask_size >= 1
                    ):
                        debit = c1.ask - p1.bid - c2.bid + p2.ask
                        profit = width - debit - self._box_comm
                        scale = min(
                            int(c1.ask_size),
                            int(p1.bid_size),
                            int(c2.bid_size),
                            int(p2.ask_size),
                            self.max_scale,
                        )
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
                        add_candidate(profit, scale, lb_legs)

                    # Short box: credit above width fully pays the maximum
                    # future loss, independent of carry.
                    if (
                        c1.bid_size >= 1
                        and p1.ask_size >= 1
                        and c2.ask_size >= 1
                        and p2.bid_size >= 1
                    ):
                        credit = c1.bid - p1.ask - c2.ask + p2.bid
                        profit = credit - width - self._box_comm
                        scale = min(
                            int(c1.bid_size),
                            int(p1.ask_size),
                            int(c2.ask_size),
                            int(p2.bid_size),
                            self.max_scale,
                        )
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
                        add_candidate(profit, scale, sb_legs)

        if not candidates:
            return

        # Prefer the highest total post-commission profit, then greedily add
        # disjoint packages so several standing arbitrages can be held at once.
        candidates.sort(key=lambda t: t[0], reverse=True)

        used_assets = set(held_assets)
        selected: List[Tuple[float, Tuple[str, ...], Tuple[Tuple[str, str, int], ...]]] = []
        orders_used = 0

        for total_profit, leg_assets, scaled_legs in candidates:
            if any(asset in used_assets for asset in leg_assets):
                continue
            if self._orders_this_session + orders_used + len(scaled_legs) > self.max_orders_per_session:
                continue

            selected.append((total_profit, leg_assets, scaled_legs))
            used_assets.update(leg_assets)
            orders_used += len(scaled_legs)

            if len(selected) >= self.max_packages_per_session:
                break

        if not selected:
            return

        for _, _, scaled_legs in selected:
            for side, asset, size in scaled_legs:
                if side == "buy":
                    oid = ctx.buy(size=size, asset=asset, tag=self._tag)
                else:
                    oid = ctx.sell(size=size, asset=asset, tag=self._tag)
                if oid is None:
                    return

        self._orders_this_session += orders_used

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
