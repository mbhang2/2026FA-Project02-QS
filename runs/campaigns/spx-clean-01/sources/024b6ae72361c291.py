"""Scan the quoted options surface for model-free fixed-payoff mispricings,
and keep a tiny long-option position when no edge is present.

The previous box scanner had the right idea -- a long box pays its width at
expiry regardless of the underlying -- but the index fallback that maintained
the exposure floor was losing money after the unfavorable fill model and
commission.  This version keeps the box scanner, extends it to model-free
vertical spreads and butterflies, and replaces the index fallback with a much
cheaper option position: buy the option with the smallest premium per session
left to expiry.  A long option can only lose its premium plus two commissions,
so this keeps the strategy in the market across sessions while minimizing the
cost of doing so when no arbitrage is present.

The scanner only acts on relationships whose *traded* bid/ask cash flow has a
strict positive edge after all fees.  If the surface is clean, the scanner
reports nothing and the cheap option position carries the exposure floor.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        scan_every: int = 4,
        max_packages_per_session: int = 2,
        size: int = 1,
        commission: float = 0.1,
        min_edge: float = 0.0,
        min_width: float = 5.0,
        max_width: float = 8000.0,
        max_horizon_sessions=None,
        min_time_left_ns: int = int(5 * 60 * 1_000_000_000),
    ):
        self.interval = interval
        self.scan_every = max(1, scan_every)
        self.max_packages_per_session = max(1, max_packages_per_session)
        self.size = size
        self.commission = commission
        self.min_edge = min_edge
        self.min_width = min_width
        self.max_width = max_width
        self.max_horizon_sessions = max_horizon_sessions
        self.min_time_left_ns = min_time_left_ns

        # Fixed fees for each package type.  Boxes have four legs and eight
        # commission charges (four entries, four settlements).  Vertical spreads
        # have two legs and four commission charges.  Butterflies have four
        # contracts on three legs and eight commission charges.
        self.box_fee = 8.0 * self.commission * self.size
        self.vertical_fee = 4.0 * self.commission * self.size
        self.butterfly_fee = 8.0 * self.commission * self.size

        self.underlying = "SPX"
        self._session = None
        self._bars_since_session = 0
        self._packages_this_session = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        if ":" in ctx.dataset_name:
            suffix = ctx.dataset_name.split(":")[-1]
            if suffix:
                self.underlying = suffix

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return

        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_since_session = 0
            self._packages_this_session = 0
        self._bars_since_session += 1

        if not ctx.actionable:
            return

        # Wait for any pending option market orders to fill.  They generally
        # fill on the next tick, so this is only a one-bar guard.
        if ctx.working_order_ids:
            return

        # Scan on the first bar of the session and then on a coarse schedule.
        need_scan = (
            self._bars_since_session == 1
            or self._bars_since_session % self.scan_every == 0
        ) and self._packages_this_session < self.max_packages_per_session

        # If we are flat, we need a cheap options position to satisfy the
        # exposure floor.  The scanner branch has priority.
        need_filler = not ctx.assets_held()

        if not (need_scan or need_filler):
            return

        quotes = ctx.options()
        if not quotes:
            return

        if need_scan:
            trade = self._scan_edges(ctx, quotes)
            if trade is not None:
                self._place_trade(ctx, trade)
                self._packages_this_session += 1
                return

        if need_filler:
            q = self._pick_cheap_option(ctx, quotes)
            if q is not None:
                ctx.buy(
                    size=self.size,
                    asset=self._asset_name(q),
                    tag="cheap-exposure",
                )

    # --- Edge scanning -------------------------------------------------------
    def _scan_edges(self, ctx, quotes):
        if ctx.session_end - ctx.ts < self.min_time_left_ns:
            return None

        by_expiry = {}
        for q in quotes:
            horizon = q.expiry_session - ctx.session
            if horizon <= 0:
                continue
            if (
                self.max_horizon_sessions is not None
                and horizon > self.max_horizon_sessions
            ):
                continue

            # Every package here requires both a buyable and sellable side on
            # every leg, so require positive quotes and size on both sides from
            # the start.
            if q.ask <= 0.0 or q.bid <= 0.0:
                continue
            if q.ask_size < self.size or q.bid_size < self.size:
                continue

            strike_map = by_expiry.setdefault(q.expiry_session, {})
            side = "call" if q.is_call else "put"
            strike_map.setdefault(q.strike, {})[side] = q

        best = None
        best_net = -1e18

        for expiry, strike_map in by_expiry.items():
            strikes = sorted(
                k for k, legs in strike_map.items()
                if "call" in legs and "put" in legs
            )
            if len(strikes) < 2:
                continue

            strike_set = set(strikes)

            for i, k1 in enumerate(strikes):
                c1 = strike_map[k1]["call"]
                p1 = strike_map[k1]["put"]

                for j in range(i + 1, len(strikes)):
                    k2 = strikes[j]
                    width = k2 - k1
                    if width < self.min_width or width > self.max_width:
                        continue

                    c2 = strike_map[k2]["call"]
                    p2 = strike_map[k2]["put"]

                    # -- Box spreads -------------------------------------------------
                    # Long box: buy C1, sell P1, sell C2, buy P2.
                    # Pays K2-K1 at expiry with certainty.
                    if (
                        c1.ask_size >= self.size
                        and p1.bid_size >= self.size
                        and c2.bid_size >= self.size
                        and p2.ask_size >= self.size
                    ):
                        cost = c1.ask - p1.bid - c2.bid + p2.ask
                        net = width - cost - self.box_fee
                        if net > best_net:
                            best_net = net
                            best = {
                                "kind": "box-long",
                                "expiry": expiry,
                                "k1": k1,
                                "k2": k2,
                                "legs": [
                                    (c1, "buy", self.size),
                                    (p1, "sell", self.size),
                                    (c2, "sell", self.size),
                                    (p2, "buy", self.size),
                                ],
                            }

                    # Short box: sell C1, buy P1, buy C2, sell P2.
                    # Profitable if the upfront credit exceeds the width.
                    if (
                        c1.bid_size >= self.size
                        and p1.ask_size >= self.size
                        and c2.ask_size >= self.size
                        and p2.bid_size >= self.size
                    ):
                        credit = c1.bid - p1.ask - c2.ask + p2.bid
                        net = credit - width - self.box_fee
                        if net > best_net:
                            best_net = net
                            best = {
                                "kind": "box-short",
                                "expiry": expiry,
                                "k1": k1,
                                "k2": k2,
                                "legs": [
                                    (c1, "sell", self.size),
                                    (p1, "buy", self.size),
                                    (c2, "buy", self.size),
                                    (p2, "sell", self.size),
                                ],
                            }

                    # -- Call verticals ---------------------------------------------
                    # Long call spread: buy K1 call, sell K2 call.
                    # Payoff is between 0 and width.
                    if c1.ask_size >= self.size and c2.bid_size >= self.size:
                        cost = c1.ask - c2.bid
                        net = -cost - self.vertical_fee
                        if net > best_net:
                            best_net = net
                            best = {
                                "kind": "call-vertical-long",
                                "expiry": expiry,
                                "k1": k1,
                                "k2": k2,
                                "legs": [
                                    (c1, "buy", self.size),
                                    (c2, "sell", self.size),
                                ],
                            }

                    # Short call spread: sell K1 call, buy K2 call.
                    # Profitable if the credit exceeds the width.
                    if c1.bid_size >= self.size and c2.ask_size >= self.size:
                        credit = c1.bid - c2.ask
                        net = credit - width - self.vertical_fee
                        if net > best_net:
                            best_net = net
                            best = {
                                "kind": "call-vertical-short",
                                "expiry": expiry,
                                "k1": k1,
                                "k2": k2,
                                "legs": [
                                    (c1, "sell", self.size),
                                    (c2, "buy", self.size),
                                ],
                            }

                    # -- Put verticals ----------------------------------------------
                    # Long put spread: buy K2 put, sell K1 put.
                    if p2.ask_size >= self.size and p1.bid_size >= self.size:
                        cost = p2.ask - p1.bid
                        net = -cost - self.vertical_fee
                        if net > best_net:
                            best_net = net
                            best = {
                                "kind": "put-vertical-long",
                                "expiry": expiry,
                                "k1": k1,
                                "k2": k2,
                                "legs": [
                                    (p2, "buy", self.size),
                                    (p1, "sell", self.size),
                                ],
                            }

                    # Short put spread: sell K2 put, buy K1 put.
                    if p2.bid_size >= self.size and p1.ask_size >= self.size:
                        credit = p2.bid - p1.ask
                        net = credit - width - self.vertical_fee
                        if net > best_net:
                            best_net = net
                            best = {
                                "kind": "put-vertical-short",
                                "expiry": expiry,
                                "k1": k1,
                                "k2": k2,
                                "legs": [
                                    (p2, "sell", self.size),
                                    (p1, "buy", self.size),
                                ],
                            }

                    # -- Equal-width butterflies ------------------------------------
                    # Long call butterfly: buy K1 call, sell 2 K2 calls, buy K3 call.
                    # A correctly-spaced butterfly has nonnegative payoff, so it
                    # is an arbitrage if its traded cost is negative after fees.
                    k3 = 2.0 * k2 - k1
                    if k3 in strike_set and k3 > k2:
                        c3 = strike_map[k3]["call"]
                        p3 = strike_map[k3]["put"]

                        if (
                            c1.ask_size >= self.size
                            and c3.ask_size >= self.size
                            and c2.bid_size >= 2 * self.size
                        ):
                            cost = c1.ask + c3.ask - 2.0 * c2.bid
                            net = -cost - self.butterfly_fee
                            if net > best_net:
                                best_net = net
                                best = {
                                    "kind": "call-butterfly",
                                    "expiry": expiry,
                                    "k1": k1,
                                    "k2": k2,
                                    "k3": k3,
                                    "legs": [
                                        (c1, "buy", self.size),
                                        (c2, "sell", 2 * self.size),
                                        (c3, "buy", self.size),
                                    ],
                                }

                        if (
                            p1.ask_size >= self.size
                            and p3.ask_size >= self.size
                            and p2.bid_size >= 2 * self.size
                        ):
                            cost = p1.ask + p3.ask - 2.0 * p2.bid
                            net = -cost - self.butterfly_fee
                            if net > best_net:
                                best_net = net
                                best = {
                                    "kind": "put-butterfly",
                                    "expiry": expiry,
                                    "k1": k1,
                                    "k2": k2,
                                    "k3": k3,
                                    "legs": [
                                        (p1, "buy", self.size),
                                        (p2, "sell", 2 * self.size),
                                        (p3, "buy", self.size),
                                    ],
                                }

        if best is not None and best_net > self.min_edge:
            return best
        return None

    # --- Cheap exposure fill-in ---------------------------------------------
    def _pick_cheap_option(self, ctx, quotes):
        best = None
        best_score = 1e18
        for q in quotes:
            horizon = q.expiry_session - ctx.session
            if horizon <= 0:
                continue
            if q.ask <= 0.0 or q.ask_size < self.size:
                continue

            # If the option expires worthless, the total cost is its premium
            # plus the entry and settlement commissions.  Normalize by the
            # number of sessions the position is held to meet the exposure
            # floor cheaply.
            total_cost = q.ask + 2.0 * self.commission * self.size
            score = total_cost / float(horizon)
            if score < best_score:
                best_score = score
                best = q
        return best

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
        parts = [trade["kind"], str(trade.get("expiry", ""))]
        for key in ("k1", "k2", "k3"):
            if key in trade:
                parts.append(self._fmt_strike(trade[key]))
        tag = "-".join(parts)

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
