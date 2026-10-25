"""A carry-aware single-package options arbitrage strategy.

Trial #12 remains the best in this campaign: carry-free vertical spreads,
equal-spaced long butterflies, and terminal-payoff box spreads, one package at
a time, held to settlement.  This version keeps that candidate set exactly and
adds one class of static convexity arbitrage that #12 did not scan:

Generalised weighted butterflies.

The no-arbitrage convexity condition on a strike ladder is not restricted to
equal-spaced wings.  For any three strikes K1 < K2 < K3, convexity requires
C(K2) <= lambda * C(K1) + (1 - lambda) * C(K3), where
lambda = (K3 - K2) / (K3 - K1).  A violation is a butterfly with a negative
cost (long) or a short butterfly whose credit exceeds its maximum possible
payoff.  With integer strike distances we can trade the exact integer weights
w1 = d2 / gcd(d1, d2), w3 = d1 / gcd(d1, d2), w2 = w1 + w3.

The box/vertical scans are unchanged from #12, so every trade #12 would have
taken remains available.  The new butterfly scan is conservative: it only fires
on integer strike distances, limits the number of contracts per leg, respects
quoted sizes on every leg, and demands a positive post-commission edge.
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
        max_bfly_weight: int = 10,
    ):
        self.interval = interval
        self.min_edge = min_edge
        self.max_wing_step = max_wing_step
        self.max_box_step = max_box_step
        self.max_expiries = max_expiries
        self.max_orders_per_session = max_orders_per_session
        self.max_bfly_weight = max_bfly_weight

        self._tag = "box-arb-plus-bfly"
        self._vert_comm = 0.4   # 2 legs * 2 fills * 0.1
        self._bfly_comm = 0.8   # equal-spaced 4 contracts * 2 fills * 0.1
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

    @staticmethod
    def _bfly_weights(d1: float, d2: float, limit: int):
        """Return integer butterfly weights for strike gaps d1 and d2.

        The exact convexity weights are proportional to (d2, d1) for the two
        wings, scaled by the gcd.  Returns (w1, w2, w3, a, b) where a and b
        are the integer gaps d1 and d2, or None if the weights are not safely
        tradeable.
        """
        a = round(d1)
        b = round(d2)
        if a <= 0 or b <= 0:
            return None
        if not math.isclose(d1, a, rel_tol=1e-7, abs_tol=1e-9):
            return None
        if not math.isclose(d2, b, rel_tol=1e-7, abs_tol=1e-9):
            return None

        g = math.gcd(a, b)
        w1 = b // g
        w3 = a // g
        w2 = w1 + w3
        if max(w1, w2, w3) > limit:
            return None
        return w1, w2, w3, a, b

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        if not ctx.actionable:
            return
        # Carry only one options package at a time.  This was the best prior
        # configuration and avoids the over-trading that diluted #13.
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

        # Unified candidate ranking by terminal profit after commission.
        best: Optional[Tuple[float, Tuple[Tuple[str, str, int], ...]]] = None

        def consider(profit: float, candidate_legs: Tuple[Tuple[str, str, int], ...]) -> None:
            nonlocal best
            if profit <= self.min_edge:
                return
            if best is None or profit > best[0]:
                best = (profit, candidate_legs)

        # ----- Carry-free vertical spreads and equal-spaced long butterflies -----
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

        # ----- Generalised weighted butterflies -----
        # Convexity violations can occur with unequal wing widths; #12 only
        # checked the equal-spaced case.  These use exact integer weights.
        for bucket_set in (call_lists, put_lists):
            for arr in bucket_set.values():
                arr.sort(key=lambda x: x.strike)
                n = len(arr)
                if n < 3:
                    continue

                for j in range(1, n - 1):
                    q2 = arr[j]
                    for i in range(max(0, j - self.max_wing_step), j):
                        q1 = arr[i]
                        d1 = q2.strike - q1.strike
                        if d1 <= 0.0:
                            continue
                        for k in range(j + 1, min(n, j + self.max_wing_step + 1)):
                            q3 = arr[k]
                            d2 = q3.strike - q2.strike
                            if d2 <= 0.0:
                                continue

                            wres = self._bfly_weights(d1, d2, self.max_bfly_weight)
                            if wres is None:
                                continue

                            w1, w2, w3, a, b = wres
                            max_pay = float(w1 * a)  # = w3 * b
                            total_contracts = w1 + w2 + w3
                            comm = 0.2 * total_contracts

                            bf_assets = (
                                self._asset_name(q1),
                                self._asset_name(q2),
                                self._asset_name(q3),
                            )

                            # Long weighted butterfly: buy wings, sell body.
                            # Non-negative payoff, so negative cost is arbitrage.
                            if (
                                q1.ask_size >= w1
                                and q3.ask_size >= w3
                                and q2.bid_size >= w2
                            ):
                                credit = w2 * q2.bid - w1 * q1.ask - w3 * q3.ask
                                profit = credit - comm
                                bf_legs = (
                                    ("buy", bf_assets[0], w1),
                                    ("sell", bf_assets[1], w2),
                                    ("buy", bf_assets[2], w3),
                                )
                                consider(profit, bf_legs)

                            # Short weighted butterfly: sell wings, buy body.
                            # Max possible obligation is max_pay; a credit above
                            # that is riskless before commissions.
                            if (
                                q1.bid_size >= w1
                                and q3.bid_size >= w3
                                and q2.ask_size >= w2
                            ):
                                credit = w1 * q1.bid + w3 * q3.bid - w2 * q2.ask
                                profit = credit - max_pay - comm
                                bf_legs = (
                                    ("sell", bf_assets[0], w1),
                                    ("buy", bf_assets[1], w2),
                                    ("sell", bf_assets[2], w3),
                                )
                                consider(profit, bf_legs)

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
                    # simulator charges no financing cost, so any debit below
                    # width is collectable at settlement after commission.
                    if (
                        c1.ask_size >= 1
                        and p1.bid_size >= 1
                        and c2.bid_size >= 1
                        and p2.ask_size >= 1
                    ):
                        debit = c1.ask - p1.bid - c2.bid + p2.ask
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
