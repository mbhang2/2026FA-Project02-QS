"""A scaled, one-package-at-a-time options arbitrage detector.

This inherits the positive findings of trial #12 (terminal-payoff long boxes)
but improves two things without falling into the over-trading that hurt #13.

1. The single best candidate is now selected by *total* post-commission profit
   after scaling to the largest whole-package size its quoted touch permits.
   Trial #12 always traded one contract per leg even when the quotes offered
   size.  Trial #13 scaled and then opened many packages greedily, which added
   a large number of marginal fills and reduced realised P&L.  This version
   takes only the single best scaled candidate while flat.

2. It adds the missing opposite side of the carry-free butterfly condition.
   A long butterfly bought at negative cost is an arbitrage, but a *short*
   butterfly whose credit exceeds the wingspan is equally riskless and was not
   present in #12/#13.  Both are checked with bid/ask sizes and commissions.

The candidate set remains carry-free verticals, equal-spaced butterflies, and
box spreads; box spreads are treated as terminal payoffs, so a long box with
debit below its terminal payoff is collected at settlement because the simulator
charges no financing cost.  Everything is traded at the quoted touch with size
checks, and positions are held to expiry.
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
        max_box_step: int = 60,
        max_expiries: int = 8,
        max_scale: int = 10,
    ):
        self.interval = interval
        self.min_edge = min_edge
        self.max_wing_step = max_wing_step
        self.max_box_step = max_box_step
        self.max_expiries = max_expiries
        self.max_scale = max_scale

        self._tag = "scaled-single-arb"
        self._vert_comm = 0.4   # 2 legs * 2 fills * 0.1
        self._bfly_comm = 0.8   # 4 leg-contracts * 2 fills * 0.1
        self._box_comm = 0.8    # 4 leg-contracts * 2 fills * 0.1

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

        # Exactly one options package at a time.  This keeps the package
        # riskless and avoids the marginal over-trading that diluted #13.
        if ctx.assets_held() or ctx.working_order_ids:
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

        best_profit: Optional[float] = None
        best_legs: Optional[Tuple[Tuple[str, str, int], ...]] = None

        def consider(
            unit_profit: float,
            scale: int,
            base_legs: Tuple[Tuple[str, str, int], ...],
        ) -> None:
            nonlocal best_profit, best_legs

            if unit_profit <= self.min_edge or scale < 1:
                return

            total_profit = unit_profit * scale
            if best_profit is None or total_profit > best_profit:
                best_profit = total_profit
                best_legs = tuple(
                    (side, asset, size * scale) for side, asset, size in base_legs
                )

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

                        # Long vertical: negative cost locks a non-negative payoff.
                        if long_buy.ask_size >= 1 and long_sell.bid_size >= 1:
                            cost = long_buy.ask - long_sell.bid
                            unit_profit = -cost - self._vert_comm
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
                            consider(unit_profit, scale, lv_legs)

                        # Short vertical: credit above the maximum future loss.
                        if short_sell.bid_size >= 1 and short_buy.ask_size >= 1:
                            credit = short_sell.bid - short_buy.ask
                            unit_profit = credit - width - self._vert_comm
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
                            consider(unit_profit, scale, sv_legs)

                    # Equal-spaced long butterfly: buy wings, sell body.
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
                        unit_profit = credit - self._bfly_comm
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
                        consider(unit_profit, scale, bf_legs)

                        # Add the missing carry-free short butterfly.
                        if q1.bid_size < 1 or q3.bid_size < 1 or q2.ask_size < 2:
                            continue
                        credit = q1.bid + q3.bid - 2.0 * q2.ask
                        # The short butterfly's maximum obligation at expiry is
                        # exactly the wingspan d1.
                        unit_profit = credit - d1 - self._bfly_comm
                        body_capacity = int(q2.ask_size) // 2
                        scale = min(
                            int(q1.bid_size),
                            int(q3.bid_size),
                            body_capacity,
                            self.max_scale,
                        )
                        bf_legs = (
                            ("sell", bf_assets[0], 1),
                            ("buy", bf_assets[1], 2),
                            ("sell", bf_assets[2], 1),
                        )
                        consider(unit_profit, scale, bf_legs)

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

                    # Long box: terminal payoff is exactly width; the simulator
                    # charges no financing cost, so any debit below width is a
                    # guaranteed terminal profit after commission.
                    if (
                        c1.ask_size >= 1
                        and p1.bid_size >= 1
                        and c2.bid_size >= 1
                        and p2.ask_size >= 1
                    ):
                        debit = c1.ask - p1.bid - c2.bid + p2.ask
                        unit_profit = width - debit - self._box_comm
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
                        consider(unit_profit, scale, lb_legs)

                    # Short box: credit above fully pays the maximum future loss.
                    if (
                        c1.bid_size >= 1
                        and p1.ask_size >= 1
                        and c2.ask_size >= 1
                        and p2.bid_size >= 1
                    ):
                        credit = c1.bid - p1.ask - c2.ask + p2.bid
                        unit_profit = credit - width - self._box_comm
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
                        consider(unit_profit, scale, sb_legs)

        if best_legs is None:
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
