"""A scaled static-arbitrage options detector, built on the best prior trial (#6).

Trial #6 found a profitable set of carry-free static inequalities: longs/shorts
vertical spreads, long equal-spaced butterflies, and long/short box spreads. This
version keeps that exact candidate selection and selection ordering, so the same
47 trades will be chosen if the surface is unchanged.

The one change is position sizing.  Trial #6 always traded one contract per leg
(and two contracts for the butterfly body) even when the quoted touch had more
size available.  Options market orders fill at the touch up to the quoted size,
so a candidate that clears the hurdle can be scaled up to the minimum quoted
size needed by all its legs without risking a partial fill.  This preserves the
selector -- candidates are still ranked by per-unit profit after commission --
and then sizes the chosen candidate by the largest whole number that every leg's
required side can support, capped by `max_scale`.

The arbitrage conditions are unchanged and deliberately carry-free:

- Long vertical: buy the valuable leg, sell the cheap leg.  A negative cost
  locks a non-negative terminal payoff, independent of carry.
- Short vertical: receive more than the maximum possible terminal loss, width.
- Long butterfly: a negative cost locks a non-negative terminal payoff.
- Long box: if debit <= 0, we are paid to hold a positive terminal payoff.
- Short box: if credit >= width, we receive more than the maximum terminal loss.

No positive-debit boxes or carry-dependent conversions are taken.  If the surface
is clean, no orders are placed.
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
        max_scale: int = 5,
    ):
        self.interval = interval
        self.min_edge = min_edge
        self.max_wing_step = max_wing_step
        self.max_expiries = max_expiries
        self.max_orders_per_session = max_orders_per_session
        self.max_scale = max_scale

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
        # Carry only one options package at a time, exactly as in trial #6.
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

        # The best object stores per-unit profit first, then the maximum whole
        # number of times the package can be executed at the quoted touch, then
        # the base leg list with trial #6's fixed 1-contract sizing.
        best: Optional[Tuple[float, int, Tuple[Tuple[str, str, int], ...]]] = None

        def consider(
            profit: float,
            scale: int,
            candidate_legs: Tuple[Tuple[str, str, int], ...],
        ) -> None:
            nonlocal best
            if profit <= self.min_edge:
                return
            # Ranking by per-unit profit, not total profit, keeps the exact
            # candidate preference order of trial #6.
            if best is None or profit > best[0]:
                best = (profit, scale, candidate_legs)

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

                        # Long vertical: buy the valuable leg, sell the cheap
                        # leg. A negative cost locks a non-negative payoff.
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
                            consider(profit, scale, lv_legs)

                        # Short vertical: receive more than the maximum future
                        # loss of the spread.
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
                            consider(profit, scale, sv_legs)

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
                        if scale < 1:
                            continue
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
                        consider(profit, scale, bf_legs)

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

                    # Long box: debit <= 0 is a pure violation and does not
                    # rely on knowing the carry.
                    if (
                        c1.ask_size >= 1
                        and p1.bid_size >= 1
                        and c2.bid_size >= 1
                        and p2.ask_size >= 1
                    ):
                        debit = c1.ask - p1.bid - c2.bid + p2.ask
                        if debit <= 0.0:
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
                            consider(profit, scale, lb_legs)

                    # Short box: credit >= width means we receive more than the
                    # maximum possible future loss.
                    if (
                        c1.bid_size >= 1
                        and p1.ask_size >= 1
                        and c2.ask_size >= 1
                        and p2.bid_size >= 1
                    ):
                        credit = c1.bid - p1.ask - c2.ask + p2.bid
                        if credit >= width:
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
                            consider(profit, scale, sb_legs)

        if best is None:
            return

        _, scale, base_legs = best
        if len(base_legs) > self.max_orders_per_session:
            return

        scaled_legs = tuple(
            (side, asset, size * scale)
            for side, asset, size in base_legs
        )

        for side, asset, size in scaled_legs:
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
