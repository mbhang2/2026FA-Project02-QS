"""Scan the quoted options surface for model-free violations that survive costs.

The strategy holds option packages to expiry, where box spreads, vertical
spreads and butterflies have deterministic or bounded payoffs. The edge is
computed from the *traded* quotes (the exact bid/ask sides the legs would hit)
minus commissions on entry and settlement. A discount factor is inferred
per expiry from the cross-section for boxes; butterflies and verticals need no
carry assumption at all.

If no option package clears costs, a single bracketed index trade per session
guarantees the exposure floor. The fallback leans on the previous session's
daily bias, then forces a trade later in the session so the strategy does not
go silent.
"""

from __future__ import annotations

from statistics import median


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        scan_every: int = 4,            # scan options every 20 minutes (4 x 5m)
        entry_bar: int = 3,             # first chance for index fallback
        force_bar: int = 8,             # force a fallback trade after this bar
        size: int = 1,
        commission: float = 0.1,
        min_edge: float = 0.0,
        min_width: float = 5.0,
        max_width: float = 4000.0,
        min_wing_width: float = 10.0,
        max_wing_width: float = 2500.0,
        wing_span: int = 4,
        vertical_span: int = 4,
        max_pairs_per_expiry: int = 10000,
        max_triples_per_expiry: int = 5000,
        max_fit_points: int = 40,
        daily_bias_min: float = 0.0,
        fallback_stop: float = 2.0,
        fallback_target: float = 2.0,
        min_time_left_ns: int = int(5 * 60 * 1_000_000_000),
    ):
        self.interval = interval
        self.scan_every = max(1, scan_every)
        self.entry_bar = max(1, entry_bar)
        self.force_bar = max(entry_bar, force_bar)
        self.size = size
        self.commission = commission
        self.min_edge = min_edge
        self.min_width = min_width
        self.max_width = max_width
        self.min_wing_width = min_wing_width
        self.max_wing_width = max_wing_width
        self.wing_span = wing_span
        self.vertical_span = vertical_span
        self.max_pairs_per_expiry = max_pairs_per_expiry
        self.max_triples_per_expiry = max_triples_per_expiry
        self.max_fit_points = max_fit_points
        self.daily_bias_min = daily_bias_min
        self.fallback_stop = fallback_stop
        self.fallback_target = fallback_target
        self.min_time_left_ns = min_time_left_ns

        # Commissions: per contract per fill.
        self.box_fee = 8.0 * self.commission * self.size
        self.butterfly_fee = 8.0 * self.commission * self.size
        self.vertical_fee = 4.0 * self.commission * self.size

        self.underlying = "SPX"
        self._session = None
        self._bars_since_session = 0
        self._fallback_done_this_session = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        # Daily bars let the fallback use the previous session's direction.
        ctx.subscribe("1d")
        if ":" in ctx.dataset_name:
            self.underlying = ctx.dataset_name.split(":")[-1] or "SPX"

    def on_bars(self, ctx) -> None:
        # We act only when our own 5m bar closed; the 1d subscription may also
        # deliver on_bars callbacks.
        if self.interval not in ctx.bars:
            return

        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_since_session = 0
            self._fallback_done_this_session = False
        self._bars_since_session += 1

        if not ctx.actionable:
            return

        # Carry any existing option package or index position until it resolves.
        if ctx.assets_held() or ctx.working_order_ids:
            return

        # Look for a costed option-package edge on a coarse schedule.
        if self._bars_since_session % self.scan_every == 0:
            best = self._scan_options(ctx)
            if best is not None:
                self._place_trade(ctx, best)
                return

        # Index fallback: one bracketed trade per session.
        if self._fallback_done_this_session:
            return
        if self._bars_since_session < self.entry_bar:
            return

        hist = ctx.history(self.interval)
        if not len(hist):
            return
        last = hist[-1]

        daily = ctx.history("1d")
        daily_bias = 0.0
        if len(daily) >= 1:
            prev_daily = daily[-1]
            daily_bias = prev_daily.close - prev_daily.open

        bar_bias = last.close - last.open

        # Before the force bar, wait if a meaningful daily bias disagrees.
        if self._bars_since_session < self.force_bar:
            if (
                daily_bias != 0.0
                and bar_bias != 0.0
                and abs(daily_bias) > self.daily_bias_min
                and daily_bias * bar_bias < 0
            ):
                return

        if bar_bias != 0.0:
            direction = 1 if bar_bias > 0 else -1
        elif daily_bias != 0.0:
            direction = 1 if daily_bias > 0 else -1
        else:
            direction = 1

        place = ctx.buy if direction > 0 else ctx.sell
        oid = place(
            size=self.size,
            stop_offset=self.fallback_stop,
            target_offset=self.fallback_target,
            tag="index-fallback",
        )
        if oid is not None:
            self._fallback_done_this_session = True

    # --- Option scanning -----------------------------------------------------
    def _scan_options(self, ctx):
        if ctx.session_end - ctx.ts < self.min_time_left_ns:
            return None

        quotes = ctx.options()
        if not quotes:
            return None

        by_expiry: dict[int, dict[float, dict[str, object]]] = {}
        for q in quotes:
            if q.expiry_session <= ctx.session:
                continue
            if q.bid <= 0.0 or q.ask <= 0.0:
                continue
            if q.bid_size < 1 or q.ask_size < 1:
                continue
            strike_map = by_expiry.setdefault(q.expiry_session, {})
            side = "call" if q.is_call else "put"
            strike_map.setdefault(q.strike, {})[side] = q

        best = None
        best_edge = -1e18

        for expiry, strike_map in by_expiry.items():
            strikes = sorted(
                k for k, legs in strike_map.items()
                if "call" in legs and "put" in legs
            )
            if len(strikes) < 2:
                continue

            best, best_edge = self._scan_verticals(expiry, strikes, strike_map, best, best_edge)
            best, best_edge = self._scan_butterflies(expiry, strikes, strike_map, best, best_edge)
            best, best_edge = self._scan_boxes(expiry, strikes, strike_map, best, best_edge)

        return best

    def _tentative(self, best, best_edge, kind, expiry, legs, edge):
        if edge > best_edge:
            trade = {"kind": kind, "expiry": expiry, "legs": legs, "edge": edge}
            return trade, edge
        return best, best_edge

    def _scan_verticals(self, expiry, strikes, sm, best, best_edge):
        n = len(strikes)
        triples = 0
        for i in range(n - 1):
            upper = min(n, i + 1 + self.vertical_span)
            for j in range(i + 1, upper):
                triples += 1
                if triples > self.max_triples_per_expiry:
                    return best, best_edge

                k1 = strikes[i]
                k2 = strikes[j]
                width = k2 - k1
                if width < self.min_width or width > self.max_width:
                    continue

                c1 = sm[k1].get("call")
                c2 = sm[k2].get("call")
                if c1 is not None and c2 is not None:
                    if c1.ask_size >= self.size and c2.bid_size >= self.size:
                        cost = c1.ask - c2.bid
                        edge = -cost - self.vertical_fee
                        if edge > self.min_edge:
                            legs = [
                                (c1, "buy", self.size),
                                (c2, "sell", self.size),
                            ]
                            best, best_edge = self._tentative(
                                best, best_edge, "vertical-call", expiry, legs, edge
                            )

                p1 = sm[k1].get("put")
                p2 = sm[k2].get("put")
                if p1 is not None and p2 is not None:
                    if p2.ask_size >= self.size and p1.bid_size >= self.size:
                        cost = p2.ask - p1.bid
                        edge = -cost - self.vertical_fee
                        if edge > self.min_edge:
                            legs = [
                                (p2, "buy", self.size),
                                (p1, "sell", self.size),
                            ]
                            best, best_edge = self._tentative(
                                best, best_edge, "vertical-put", expiry, legs, edge
                            )

        return best, best_edge

    def _scan_butterflies(self, expiry, strikes, sm, best, best_edge):
        n = len(strikes)
        triples = 0
        for j in range(1, n - 1):
            i_low = max(0, j - self.wing_span)
            l_high = min(n, j + self.wing_span + 1)
            for i in range(i_low, j):
                for l in range(j + 1, l_high):
                    triples += 1
                    if triples > self.max_triples_per_expiry:
                        return best, best_edge

                    k1 = strikes[i]
                    k2 = strikes[j]
                    k3 = strikes[l]
                    w1 = k2 - k1
                    w2 = k3 - k2
                    if w1 < self.min_wing_width or w2 < self.min_wing_width:
                        continue
                    if w1 > self.max_wing_width or w2 > self.max_wing_width:
                        continue

                    c1 = sm[k1].get("call")
                    c2 = sm[k2].get("call")
                    c3 = sm[k3].get("call")
                    if c1 is not None and c2 is not None and c3 is not None:
                        # Long call butterfly: buy wings, sell 2 x middle.
                        if (
                            c1.ask_size >= self.size
                            and c3.ask_size >= self.size
                            and c2.bid_size >= 2 * self.size
                        ):
                            cost = c1.ask + c3.ask - 2.0 * c2.bid
                            edge = -cost - self.butterfly_fee
                            if edge > self.min_edge:
                                legs = [
                                    (c1, "buy", self.size),
                                    (c3, "buy", self.size),
                                    (c2, "sell", 2 * self.size),
                                ]
                                best, best_edge = self._tentative(
                                    best, best_edge, "call-butterfly-long", expiry, legs, edge
                                )

                        # Short call butterfly: sell wings, buy 2 x middle.
                        if (
                            c1.bid_size >= self.size
                            and c3.bid_size >= self.size
                            and c2.ask_size >= 2 * self.size
                        ):
                            credit = c1.bid + c3.bid - 2.0 * c2.ask
                            max_payoff = max(w1, w2 - w1, 0.0)
                            edge = credit - max_payoff - self.butterfly_fee
                            if edge > self.min_edge:
                                legs = [
                                    (c1, "sell", self.size),
                                    (c3, "sell", self.size),
                                    (c2, "buy", 2 * self.size),
                                ]
                                best, best_edge = self._tentative(
                                    best, best_edge, "call-butterfly-short", expiry, legs, edge
                                )

                    p1 = sm[k1].get("put")
                    p2 = sm[k2].get("put")
                    p3 = sm[k3].get("put")
                    if p1 is not None and p2 is not None and p3 is not None:
                        # Long put butterfly: buy wings, sell 2 x middle.
                        if (
                            p1.ask_size >= self.size
                            and p3.ask_size >= self.size
                            and p2.bid_size >= 2 * self.size
                        ):
                            cost = p1.ask + p3.ask - 2.0 * p2.bid
                            edge = -cost - self.butterfly_fee
                            if edge > self.min_edge:
                                legs = [
                                    (p1, "buy", self.size),
                                    (p3, "buy", self.size),
                                    (p2, "sell", 2 * self.size),
                                ]
                                best, best_edge = self._tentative(
                                    best, best_edge, "put-butterfly-long", expiry, legs, edge
                                )

                        # Short put butterfly: sell wings, buy 2 x middle.
                        if (
                            p1.bid_size >= self.size
                            and p3.bid_size >= self.size
                            and p2.ask_size >= 2 * self.size
                        ):
                            credit = p1.bid + p3.bid - 2.0 * p2.ask
                            max_payoff = max(w2, w2 - w1, 0.0)
                            edge = credit - max_payoff - self.butterfly_fee
                            if edge > self.min_edge:
                                legs = [
                                    (p1, "sell", self.size),
                                    (p3, "sell", self.size),
                                    (p2, "buy", 2 * self.size),
                                ]
                                best, best_edge = self._tentative(
                                    best, best_edge, "put-butterfly-short", expiry, legs, edge
                                )

        return best, best_edge

    def _scan_boxes(self, expiry, strikes, sm, best, best_edge):
        if len(strikes) < 2:
            return best, best_edge

        fair_df = self._fair_df_for_expiry(strikes, sm)
        if fair_df is None:
            return best, best_edge

        pair_count = 0
        for i, k1 in enumerate(strikes):
            c1 = sm[k1]["call"]
            p1 = sm[k1]["put"]
            for j in range(i + 1, len(strikes)):
                k2 = strikes[j]
                width = k2 - k1
                if width < self.min_width or width > self.max_width:
                    continue
                pair_count += 1
                if pair_count > self.max_pairs_per_expiry:
                    return best, best_edge

                c2 = sm[k2]["call"]
                p2 = sm[k2]["put"]

                # Long box: buy C1, sell P1, sell C2, buy P2.
                if (
                    c1.ask_size >= self.size
                    and p1.bid_size >= self.size
                    and c2.bid_size >= self.size
                    and p2.ask_size >= self.size
                ):
                    cost = c1.ask - p1.bid - c2.bid + p2.ask
                    edge = (fair_df * width - cost) - self.box_fee
                    if edge > self.min_edge:
                        legs = [
                            (c1, "buy", self.size),
                            (p1, "sell", self.size),
                            (c2, "sell", self.size),
                            (p2, "buy", self.size),
                        ]
                        best, best_edge = self._tentative(
                            best, best_edge, "box-long", expiry, legs, edge
                        )

                # Short box: sell C1, buy P1, buy C2, sell P2.
                if (
                    c1.bid_size >= self.size
                    and p1.ask_size >= self.size
                    and c2.ask_size >= self.size
                    and p2.bid_size >= self.size
                ):
                    credit = c1.bid - p1.ask - c2.ask + p2.bid
                    edge = (credit - fair_df * width) - self.box_fee
                    if edge > self.min_edge:
                        legs = [
                            (c1, "sell", self.size),
                            (p1, "buy", self.size),
                            (c2, "buy", self.size),
                            (p2, "sell", self.size),
                        ]
                        best, best_edge = self._tentative(
                            best, best_edge, "box-short", expiry, legs, edge
                        )

        return best, best_edge

    def _fair_df_for_expiry(self, strikes, sm):
        """Infer an expiry's discount factor from C_mid - P_mid vs strike.

        C_mid - P_mid = df * (F - K), so the pairwise slope against strike is
        -df. The median pairwise slope is robust to a single bad quote.
        """
        points = []
        for k in strikes:
            c = sm[k]["call"]
            p = sm[k]["put"]
            points.append((float(k), c.mid - p.mid))

        if len(points) < 2:
            return None

        if len(points) > self.max_fit_points:
            stride = len(points) // self.max_fit_points
            points = points[::stride]

        slopes = []
        for i in range(len(points)):
            x1, y1 = points[i]
            for j in range(i + 1, len(points)):
                x2, y2 = points[j]
                dx = x2 - x1
                if dx == 0:
                    continue
                slopes.append((y2 - y1) / dx)
        if not slopes:
            return None

        fair_df = -median(slopes)
        if fair_df <= 0.0:
            return None
        return fair_df

    # --- Order placement -----------------------------------------------------
    def _fmt_strike(self, strike) -> str:
        f = float(strike)
        if f.is_integer():
            return str(int(f))
        s = f"{f:.2f}".rstrip("0").rstrip(".")
        return s or "0"

    def _asset_name(self, quote) -> str:
        return (
            f"{self.underlying}-{int(quote.expiry_session)}-"
            f"{'C' if quote.is_call else 'P'}-{self._fmt_strike(quote.strike)}"
        )

    def _place_trade(self, ctx, trade) -> None:
        tag = f"{trade['kind']}-{trade['expiry']}"
        for quote, side, qty in trade["legs"]:
            asset = self._asset_name(quote)
            if side == "buy":
                ctx.buy(size=qty, asset=asset, tag=tag)
            else:
                ctx.sell(size=qty, asset=asset, tag=tag)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
