"""A term-adjusted single-package options arbitrage strategy.

Trial #12 remains the best in this campaign: it keeps carry-free verticals,
equal-spaced butterflies, and both long terminal-payoff boxes and extreme
short boxes, one package at a time, size 1, and holds to settlement.

This version makes one small change: candidates are ranked by **terminal
profit per session-to-expiry** rather than by raw terminal profit.  Every
package considered here is held to expiry, so a larger far-dated box can tie
up the single-package slot for many sessions and crowd out several shorter,
near-dated positions that would collectively return more.  Ranking by
`profit / sessions_to_expiry` prefers candidates that recycle capital faster
while still demanding a positive per-unit edge after commission.

The candidate set and all size/touch checks are otherwise identical to #12:
``max_wing_step=4``, ``max_box_step=40``, ``max_expiries=6``.  One package at a
time, one contract per leg, no scaling, no overlapping packages.
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
        max_expiries: int = 6,
        max_orders_per_session: int = 12,
    ):
        self.interval = interval
        self.min_edge = min_edge
        self.max_wing_step = max_wing_step
        self.max_box_step = max_box_step
        self.max_expiries = max_expiries
        self.max_orders_per_session = max_orders_per_session

        self._tag = "term-adjusted-arb"
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
        # One options package at a time, exactly as in the best prior trial.
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

        # Best is stored as (profit_per_session, profit, legs).
        best: Optional[Tuple[float, float, Tuple[Tuple[str, str, int], ...]]] = None

        def consider(
            profit: float,
            sessions_to_expiry: int,
            candidate_legs: Tuple[Tuple[str, str, int], ...],
        ) -> None:
            nonlocal best
            if profit <= self.min_edge:
                return

            # A package held to expiry ties up the single-package slot for
            # this many sessions.  Prefer the best profit per session held.
            hold = max(1, int(sessions_to_expiry))
            score = profit / hold

            if best is None or score > best[0] or (score == best[0] and profit > best[1]):
                best = (score, profit, candidate_legs)

        # ----- Carry-free vertical spreads and equal-spaced butterflies -----
        for bucket_set in (call_lists, put_lists):
            for exp, arr in bucket_set.items():
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

                        # Long vertical: a negative cost locks a non-negative
                        # payoff.
                        if long_buy.ask_size >= 1 and long_sell.bid_size >= 1:
                            cost = long_buy.ask - long_sell.bid
                            unit_profit = -cost - self._vert_comm
                            lv_assets = (
                                self._asset_name(long_buy),
                                self._asset_name(long_sell),
                            )
                            lv_legs = (
                                ("buy", lv_assets[0], 1),
                                ("sell", lv_assets[1], 1),
                            )
                            consider(unit_profit, exp - ctx.session, lv_legs)

                        # Short vertical: credit above the maximum future loss.
                        if short_sell.bid_size >= 1 and short_buy.ask_size >= 1:
                            credit = short_sell.bid - short_buy.ask
                            unit_profit = credit - width - self._vert_comm
                            sv_assets = (
                                self._asset_name(short_sell),
                                self._asset_name(short_buy),
                            )
                            sv_legs = (
                                ("sell", sv_assets[0], 1),
                                ("buy", sv_assets[1], 1),
                            )
                            consider(unit_profit, exp - ctx.session, sv_legs)

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
                        consider(unit_profit, exp - ctx.session, bf_legs)

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

                    # Long box: terminal payoff is exactly width.  The
                    # simulator charges no financing cost, so a debit below
                    # width is collectable at settlement after commission.
                    if (
                        c1.ask_size >= 1
                        and p1.bid_size >= 1
                        and c2.bid_size >= 1
                        and p2.ask_size >= 1
                    ):
                        debit = c1.ask - p1.bid - c2.bid + p2.ask
                        unit_profit = width - debit - self._box_comm
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
                        consider(unit_profit, exp - ctx.session, lb_legs)

                    # Short box: credit above width fully pays the maximum
                    # future loss, independent of carry.
                    if (
                        c1.bid_size >= 1
                        and p1.ask_size >= 1
                        and c2.ask_size >= 1
                        and p2.bid_size >= 1
                    ):
                        credit = c1.bid - p1.ask - c2.ask + p2.bid
                        unit_profit = credit - width - self._box_comm
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
                        consider(unit_profit, exp - ctx.session, sb_legs)

        if best is None:
            return

        _, _, best_legs = best
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
