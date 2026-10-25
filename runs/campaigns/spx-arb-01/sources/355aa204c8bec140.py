"""A carry-aware options arbitrage scan extended with generic strike convexity.

Trial #12 remains the best candidate set so far: carry-free vertical spreads,
equal-spaced long butterflies, and terminal-payoff box spreads, one package at
a time, held to settlement.  #18 added size scaling and expiration-density
ranking without changing the score.  #19's relative box layer was harmful and
is deliberately not retained.

This version keeps #12/#18's profitable candidates exactly in spirit and adds
one additional class of static, carry-free relationship:

    **Generic convexity.**  For European index options, the call price and the
    put price are each convex in strike.  For strikes K1 < K2 < K3 with weights
    proportional to (K3-K2, K1+K3-2*K2, K2-K1), the portfolio
        w1 * buy outer low, w2 * sell middle, w3 * buy outer high
    has a non-negative terminal payoff for both calls and puts.  Its reverse has
    a bounded maximum terminal loss.  This object had previously only been
    scanned for the special case of equal-spaced strikes (1:2:1); the generic
    scan allows unequal strike spacing and scaled-down integer weights.

The earlier equal-spaced long-butterfly scan is retained for exact
compatibility.  Vertical spreads and box spreads are widened modestly, because
their carry-free or terminal-payoff arguments remain valid across wider strike
pairs.  The relative box-spread layer from #19 is omitted.

Everything remains one options package at a time, traded at the quoted touch
with size checks, and held to expiry so settlement converts the locked payoff
into realized P&L.  Candidates are ranked by profit per session held, then
total profit, then shortest time to settlement.
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
        max_conv_span: int = 8,
        max_box_step: int = 200,
        max_expiries: int = 10,
        max_package_size: int = 10,
        max_orders_per_session: int = 12,
    ):
        self.interval = interval
        self.min_edge = min_edge
        self.max_wing_step = max_wing_step
        self.max_conv_span = max_conv_span
        self.max_box_step = max_box_step
        self.max_expiries = max_expiries
        self.max_package_size = max_package_size
        self.max_orders_per_session = max_orders_per_session

        self._tag = "arb-generic-conv"
        self._vert_comm_per_unit = 0.4   # 2 legs * (entry + settlement) * 0.1
        self._bfly_comm_per_unit = 0.8   # 4 contracts * 2 fills * 0.1
        self._box_comm_per_unit = 0.8    # 4 legs * 2 fills * 0.1
        self._per_contract_full = 0.2    # entry 0.1 + settlement 0.1

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
        # One package at a time.  Prior best configurations show this focus is
        # worth more than holding overlapping packages.
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

                        # Long vertical: a negative cost locks a non-negative
                        # terminal payoff.
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
                    # A negative cost locks a non-negative payoff.  Kept for
                    # exact continuity with the best prior candidates.
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

        # ----- Generic strike convexity for calls and puts -----
        for bucket_set in (call_lists, put_lists):
            for arr in bucket_set.values():
                arr.sort(key=lambda x: x.strike)
                n = len(arr)
                if n < 3:
                    continue

                max_span = min(self.max_conv_span, n - 1)

                for i in range(n - 2):
                    for j in range(i + 1, min(n - 1, i + max_span)):
                        for k in range(j + 1, min(n, i + max_span + 1)):
                            q1 = arr[i]
                            q2 = arr[j]
                            q3 = arr[k]

                            d1 = q2.strike - q1.strike
                            d2 = q3.strike - q2.strike
                            if d1 <= 0.0 or d2 <= 0.0:
                                continue

                            # Integer weights are required for trade size.
                            d1i = int(round(d1))
                            d2i = int(round(d2))
                            if not math.isclose(d1, d1i, rel_tol=1e-6, abs_tol=1e-9):
                                continue
                            if not math.isclose(d2, d2i, rel_tol=1e-6, abs_tol=1e-9):
                                continue

                            g = math.gcd(math.gcd(d1i, d2i), d1i + d2i)
                            if g <= 0:
                                continue

                            w1 = d2i // g
                            w2 = (d1i + d2i) // g
                            w3 = d1i // g
                            if w1 <= 0 or w2 <= 0 or w3 <= 0:
                                continue

                            total_contracts = w1 + w2 + w3
                            comm_per_unit = self._per_contract_full * total_contracts
                            max_payoff = (d1 * d2) / g

                            # Long convexity: terminal payoff is non-negative.
                            avail_long = int(
                                min(
                                    q1.ask_size // w1,
                                    q2.bid_size // w2,
                                    q3.ask_size // w3,
                                )
                            )
                            units_long = min(avail_long, self.max_package_size)
                            if units_long >= 1:
                                credit_long = (
                                    w2 * q2.bid - w1 * q1.ask - w3 * q3.ask
                                )
                                unit_profit_long = credit_long - comm_per_unit
                                long_assets = (
                                    self._asset_name(q1),
                                    self._asset_name(q2),
                                    self._asset_name(q3),
                                )
                                long_base = (
                                    ("buy", long_assets[0], w1),
                                    ("sell", long_assets[1], w2),
                                    ("buy", long_assets[2], w3),
                                )
                                consider(unit_profit_long, units_long, arr[0].expiry_session, long_base)

                            # Short convexity: terminal payoff is non-positive,
                            # with bounded maximum loss `max_payoff`.
                            avail_short = int(
                                min(
                                    q1.bid_size // w1,
                                    q2.ask_size // w2,
                                    q3.bid_size // w3,
                                )
                            )
                            units_short = min(avail_short, self.max_package_size)
                            if units_short >= 1:
                                credit_short = (
                                    w1 * q1.bid + w3 * q3.bid - w2 * q2.ask
                                )
                                unit_profit_short = (
                                    credit_short - max_payoff - comm_per_unit
                                )
                                short_assets = (
                                    self._asset_name(q1),
                                    self._asset_name(q2),
                                    self._asset_name(q3),
                                )
                                short_base = (
                                    ("sell", short_assets[0], w1),
                                    ("buy", short_assets[1], w2),
                                    ("sell", short_assets[2], w3),
                                )
                                consider(unit_profit_short, units_short, arr[0].expiry_session, short_base)

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

                    # Long box: the terminal payoff is exactly width.  The
                    # simulator charges no financing cost, so any debit below
                    # width is a guaranteed terminal profit after commissions.
                    avail_lb = int(
                        min(c1.ask_size, p1.bid_size, c2.bid_size, p2.ask_size)
                    )
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
                    avail_sb = int(
                        min(c1.bid_size, p1.ask_size, c2.ask_size, p2.bid_size)
                    )
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
