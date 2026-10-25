"""A static-arbitrage scanner for a quoted options surface.

The strategy subscribes to one-minute bars and, on every bar where it has no
position and no working orders, scans the whole cross-section returned by
ctx.options().  It looks for relationships whose *quoted* bid/ask prices are
enough out of line that the package can be bought for a credit after
commissions, or sold for a credit larger than the package's maximum possible
payout.  Every trade is therefore a deterministic, non-negative-payoff package
held to expiration; the engine's cash settlement unwinds it.

The relations checked are all static and do not require knowing the riskless
rate / carry:

- vertical call and put spreads (payoff bounds 0 .. K2-K1)
- three-strike call and put ladders, size 1/1/1
- equal-spaced call and put butterflies
- boxes between adjacent strikes that exist for both calls and puts

All candidate edges are computed from executable prices: buys pay the ask,
sells receive the bid, and commission (0.1 per contract per side) is included.
Sides with quoted size zero are ignored.  If no edge survives the cost model,
the strategy reports/trades nothing, which is a deliberate part of the
campaign question: a detector that cannot report clean surfaces is useless.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, *, interval: str = "1m", min_edge: float = 0.10, size: int = 1):
        self.interval = interval
        self.min_edge = min_edge
        self.size = size
        self._in_position = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval, emit_empty=True)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # If we think we have a package on, wait until it settles away.
        if self._in_position:
            if not ctx.working_order_ids and not ctx.assets_held():
                self._in_position = False
            else:
                return

        if ctx.working_order_ids or ctx.position or ctx.assets_held():
            return

        quotes = ctx.options()
        if not quotes:
            return

        best = self._find_best_arb(quotes, ctx.session)
        if best is None:
            return

        self._place_package(ctx, best)

    def on_fill(self, ctx) -> None:
        # The static packages are held to expiration and settled by the engine.
        pass

    def on_lifecycle(self, ctx) -> None:
        # No order ids are kept across callbacks; nothing to clean up.
        pass

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _asset_name(self, q):
        strike = q.strike
        if abs(strike - round(strike)) < 1e-9:
            strike_text = str(int(round(strike)))
        else:
            strike_text = f"{strike:.6f}".rstrip("0").rstrip(".")
        return f"SPX-{q.expiry_session}-{'C' if q.is_call else 'P'}-{strike_text}"

    def _leg(self, q, action, size):
        return {"asset": self._asset_name(q), "action": action, "size": size}

    def _place_package(self, ctx, package):
        self._in_position = True
        for leg in package["legs"]:
            if leg["action"] == "buy":
                ctx.buy(size=leg["size"], asset=leg["asset"], tag="static-arb")
            else:
                ctx.sell(size=leg["size"], asset=leg["asset"], tag="static-arb")

    def _find_best_arb(self, quotes, session):
        grouped = {}
        for q in quotes:
            if q.expiry_session < session:
                continue
            grouped.setdefault(q.expiry_session, []).append(q)

        best = None

        def consider(edge, legs, desc):
            nonlocal best
            if edge is not None and edge > self.min_edge:
                if best is None or edge > best["edge"]:
                    best = {"edge": edge, "legs": legs, "desc": desc}

        fee_v = 0.2       # two contracts
        fee_l = 0.3       # three contracts
        fee_b = 0.4       # four contracts
        size = self.size

        for _, qs in grouped.items():
            calls = sorted([q for q in qs if q.is_call], key=lambda q: q.strike)
            puts = sorted([q for q in qs if not q.is_call], key=lambda q: q.strike)

            # Vertical call spreads, adjacent strikes.
            for i in range(len(calls) - 1):
                q1, q2 = calls[i], calls[i + 1]

                # Buy lower, sell higher: non-negative payoff.
                if q1.ask_size >= size and q2.bid_size >= size:
                    cost = q1.ask - q2.bid + fee_v
                    if cost < 0:
                        consider(
                            -cost,
                            [self._leg(q1, "buy", size), self._leg(q2, "sell", size)],
                            "long-call-vertical",
                        )

                # Sell lower, buy higher: max payout K2-K1.
                if q1.bid_size >= size and q2.ask_size >= size:
                    credit = q1.bid - q2.ask
                    edge = credit - fee_v - (q2.strike - q1.strike)
                    if edge > 0:
                        consider(
                            edge,
                            [self._leg(q1, "sell", size), self._leg(q2, "buy", size)],
                            "short-call-vertical",
                        )

            # Vertical put spreads, adjacent strikes.
            for i in range(len(puts) - 1):
                q1, q2 = puts[i], puts[i + 1]  # q1 lower, q2 higher

                # Buy higher, sell lower: non-negative payoff.
                if q2.ask_size >= size and q1.bid_size >= size:
                    cost = q2.ask - q1.bid + fee_v
                    if cost < 0:
                        consider(
                            -cost,
                            [self._leg(q1, "sell", size), self._leg(q2, "buy", size)],
                            "long-put-vertical",
                        )

                # Sell higher, buy lower: max payout K2-K1.
                if q2.bid_size >= size and q1.ask_size >= size:
                    credit = q2.bid - q1.ask
                    edge = credit - fee_v - (q2.strike - q1.strike)
                    if edge > 0:
                        consider(
                            edge,
                            [self._leg(q1, "buy", size), self._leg(q2, "sell", size)],
                            "short-put-vertical",
                        )

            # Three-strike call ladders and butterflies, adjacent triples.
            for i in range(len(calls) - 2):
                q1, q2, q3 = calls[i], calls[i + 1], calls[i + 2]

                # +1/-1/+1 call ladder: non-negative payoff, unbounded upside.
                if q1.ask_size >= size and q2.bid_size >= size and q3.ask_size >= size:
                    cost = q1.ask - q2.bid + q3.ask + fee_l
                    if cost < 0:
                        consider(
                            -cost,
                            [
                                self._leg(q1, "buy", size),
                                self._leg(q2, "sell", size),
                                self._leg(q3, "buy", size),
                            ],
                            "long-call-ladder",
                        )

                # Equal-spaced butterfly, where k2-k1 == k3-k2.
                d1 = q2.strike - q1.strike
                d2 = q3.strike - q2.strike
                if abs(d1 - d2) < 1e-9:
                    if q1.ask_size >= size and q2.bid_size >= 2 * size and q3.ask_size >= size:
                        cost = q1.ask - 2.0 * q2.bid + q3.ask + fee_b
                        if cost < 0:
                            consider(
                                -cost,
                                [
                                    self._leg(q1, "buy", size),
                                    self._leg(q2, "sell", 2 * size),
                                    self._leg(q3, "buy", size),
                                ],
                                "long-call-butterfly",
                            )

            # Three-strike put ladders and butterflies.
            for i in range(len(puts) - 2):
                q1, q2, q3 = puts[i], puts[i + 1], puts[i + 2]

                # +1/-1/+1 put ladder: non-negative payoff on S>=0.
                if q1.ask_size >= size and q2.bid_size >= size and q3.ask_size >= size:
                    cost = q1.ask - q2.bid + q3.ask + fee_l
                    if cost < 0:
                        consider(
                            -cost,
                            [
                                self._leg(q1, "buy", size),
                                self._leg(q2, "sell", size),
                                self._leg(q3, "buy", size),
                            ],
                            "long-put-ladder",
                        )

                d1 = q2.strike - q1.strike
                d2 = q3.strike - q2.strike
                if abs(d1 - d2) < 1e-9:
                    if q1.ask_size >= size and q2.bid_size >= 2 * size and q3.ask_size >= size:
                        cost = q1.ask - 2.0 * q2.bid + q3.ask + fee_b
                        if cost < 0:
                            consider(
                                -cost,
                                [
                                    self._leg(q1, "buy", size),
                                    self._leg(q2, "sell", 2 * size),
                                    self._leg(q3, "buy", size),
                                ],
                                "long-put-butterfly",
                            )

            # Boxes between adjacent strikes that have both a call and a put.
            call_by_strike = {q.strike: q for q in calls}
            put_by_strike = {q.strike: q for q in puts}
            common_strikes = sorted(set(call_by_strike).intersection(set(put_by_strike)))

            for i in range(len(common_strikes) - 1):
                k1, k2 = common_strikes[i], common_strikes[i + 1]
                c1, c2 = call_by_strike[k1], call_by_strike[k2]
                p1, p2 = put_by_strike[k1], put_by_strike[k2]

                # Long box: buy C1, sell P1, sell C2, buy P2.  Payoff K2-K1.
                if c1.ask_size >= size and p1.bid_size >= size and c2.bid_size >= size and p2.ask_size >= size:
                    cost = c1.ask + p2.ask - p1.bid - c2.bid + fee_b
                    if cost < 0:
                        consider(
                            -cost,
                            [
                                self._leg(c1, "buy", size),
                                self._leg(p1, "sell", size),
                                self._leg(c2, "sell", size),
                                self._leg(p2, "buy", size),
                            ],
                            "long-box",
                        )

                # Short box: sell C1, buy P1, buy C2, sell P2.  Max loss K2-K1.
                if c1.bid_size >= size and p1.ask_size >= size and c2.ask_size >= size and p2.bid_size >= size:
                    credit = c1.bid + p2.bid - p1.ask - c2.ask
                    edge = credit - fee_b - (k2 - k1)
                    if edge > 0:
                        consider(
                            edge,
                            [
                                self._leg(c1, "sell", size),
                                self._leg(p1, "buy", size),
                                self._leg(c2, "buy", size),
                                self._leg(p2, "sell", size),
                            ],
                            "short-box",
                        )

        return best
