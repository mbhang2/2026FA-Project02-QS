"""Hybrid fixed-payoff box floor + index fallback.

The prior box scanners only acted when a spread cleared a positive edge and
otherwise fell back to the underlying, which was the main source of losses.
This version scans the quoted surface *before* the index fallback and is
willing to use a box spread that is merely "least bad" when it is flat: a
box has a fixed, fully collateralised payoff, and its per-session cost after
fees can be substantially lower than the cost of repeatedly crossing the
underlying spread.  Positive box/vertical edges still take priority.

The key change is that the options scanner now has a chance to run on the
first bars of a session, before `entry_bar` forces an index trade.  Previous
versions often fell back to the index before the first scheduled scan.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        scan_every: int = 1,
        max_packages_per_session: int = 1,
        size: int = 1,
        commission: float = 0.1,
        min_edge: float = 0.0,
        min_width: float = 5.0,
        max_width: float = 8000.0,
        max_horizon_sessions=None,
        min_time_left_ns: int = int(5 * 60 * 1_000_000_000),
        floor_min_score: float = -0.30,
        floor_min_net: float = -2.0,
        min_floor_horizon: int = 2,
        entry_bar: int = 3,
        force_bar: int = 8,
        daily_bias_min: float = 0.0,
        fallback_stop: float = 2.0,
        fallback_target: float = 2.0,
        fallback_skip_every: int = 4,
        fallback_skip_rem: int = 0,
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
        self.floor_min_score = floor_min_score
        self.floor_min_net = floor_min_net
        self.min_floor_horizon = min_floor_horizon
        self.entry_bar = max(1, entry_bar)
        self.force_bar = max(entry_bar, force_bar)
        self.daily_bias_min = daily_bias_min
        self.fallback_stop = fallback_stop
        self.fallback_target = fallback_target
        self.fallback_skip_every = max(1, fallback_skip_every)
        self.fallback_skip_rem = fallback_skip_rem % self.fallback_skip_every

        # Four entry legs plus four settlement fills, per contract per fill.
        self.box_fee = 8.0 * self.commission * self.size
        self.vertical_fee = 4.0 * self.commission * self.size

        self.underlying = "SPX"
        self._session = None
        self._bars_since_session = 0
        self._fallback_done_this_session = False
        self._packages_this_session = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        # Daily bars let the index fallback lean on the previous session bias.
        ctx.subscribe("1d")
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
            self._fallback_done_this_session = False
            self._packages_this_session = 0
        self._bars_since_session += 1

        if not ctx.actionable:
            return

        # Let multi-leg option market orders finish before doing anything else.
        if ctx.working_order_ids:
            return

        # Already carrying an option package or an index position.  A box held
        # to expiry satisfies the exposure floor across many sessions, so do
        # not pile more risk on top of it.
        if ctx.assets_held():
            return

        # Try the options surface before the index fallback has a chance to
        # force an underlying trade.  Scanning on the first flat bar matters:
        # previous versions often reached `entry_bar` before their first scan.
        if (
            self._packages_this_session < self.max_packages_per_session
            and (self._bars_since_session == 1 or self._bars_since_session % self.scan_every == 0)
            and ctx.session_end - ctx.ts >= self.min_time_left_ns
        ):
            quotes = ctx.options()
            if quotes:
                by_expiry = self._build_map(ctx, quotes)
                if by_expiry:
                    trade = self._scan_edges(ctx, by_expiry)
                    if trade is not None:
                        self._place_trade(ctx, trade)
                        self._packages_this_session += 1
                        return

                    floor = self._scan_floor_box(ctx, by_expiry)
                    if floor is not None and floor["score"] >= self.floor_min_score:
                        self._place_trade(ctx, floor)
                        self._packages_this_session += 1
                        return

        # No acceptable options position.  Use the same index fallback as the
        # best previous trial, which at least met the exposure floor.
        self._maybe_index_fallback(ctx)

    # --- Quote organisation --------------------------------------------------
    def _build_map(self, ctx, quotes):
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
            if q.bid <= 0.0 or q.ask <= 0.0:
                continue
            if q.bid_size < self.size or q.ask_size < self.size:
                continue

            strike_map = by_expiry.setdefault(q.expiry_session, {})
            side = "call" if q.is_call else "put"
            strike_map.setdefault(q.strike, {})[side] = q
        return by_expiry

    # --- Edge scanner --------------------------------------------------------
    def _scan_edges(self, ctx, by_expiry):
        best = None
        best_net = -1e18

        for expiry, strike_map in by_expiry.items():
            strikes = sorted(
                k for k, legs in strike_map.items()
                if "call" in legs and "put" in legs
            )
            if len(strikes) < 2:
                continue

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

                    # Long box: buy C1, sell P1, sell C2, buy P2.
                    # At expiry it pays K2-K1 with certainty.
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

                    # Long call vertical: buy K1 call, sell K2 call.
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

                    # Short call vertical: sell K1 call, buy K2 call.
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

                    # Long put vertical: buy K2 put, sell K1 put.
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

                    # Short put vertical: sell K2 put, buy K1 put.
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

        if best is not None and best_net > self.min_edge:
            return best
        return None

    # --- Least-bad box for the exposure floor --------------------------------
    def _scan_floor_box(self, ctx, by_expiry):
        best = None
        best_score = -1e18

        for expiry, strike_map in by_expiry.items():
            horizon = expiry - ctx.session
            if horizon < self.min_floor_horizon:
                continue

            strikes = sorted(
                k for k, legs in strike_map.items()
                if "call" in legs and "put" in legs
            )
            if len(strikes) < 2:
                continue

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

                    if (
                        c1.ask_size >= self.size
                        and p1.bid_size >= self.size
                        and c2.bid_size >= self.size
                        and p2.ask_size >= self.size
                    ):
                        cost = c1.ask - p1.bid - c2.bid + p2.ask
                        net = width - cost - self.box_fee
                        if net >= self.floor_min_net:
                            score = net / float(horizon)
                            if score > best_score:
                                best_score = score
                                best = {
                                    "kind": "box-long",
                                    "expiry": expiry,
                                    "k1": k1,
                                    "k2": k2,
                                    "net": net,
                                    "score": score,
                                    "legs": [
                                        (c1, "buy", self.size),
                                        (p1, "sell", self.size),
                                        (c2, "sell", self.size),
                                        (p2, "buy", self.size),
                                    ],
                                }

                    if (
                        c1.bid_size >= self.size
                        and p1.ask_size >= self.size
                        and c2.ask_size >= self.size
                        and p2.bid_size >= self.size
                    ):
                        credit = c1.bid - p1.ask - c2.ask + p2.bid
                        net = credit - width - self.box_fee
                        if net >= self.floor_min_net:
                            score = net / float(horizon)
                            if score > best_score:
                                best_score = score
                                best = {
                                    "kind": "box-short",
                                    "expiry": expiry,
                                    "k1": k1,
                                    "k2": k2,
                                    "net": net,
                                    "score": score,
                                    "legs": [
                                        (c1, "sell", self.size),
                                        (p1, "buy", self.size),
                                        (c2, "buy", self.size),
                                        (p2, "sell", self.size),
                                    ],
                                }

        return best

    # --- Index fallback ------------------------------------------------------
    def _maybe_index_fallback(self, ctx) -> None:
        if ctx.assets_held():
            return
        if self._fallback_done_this_session:
            return
        if ctx.session % self.fallback_skip_every == self.fallback_skip_rem:
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

        # Before the force bar, defer if a meaningful daily bias disagrees.
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
        for key in ("k1", "k2"):
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
