"""Floor widening: scan options first, fall back to the index only if no
bounded-risk option package clears the floor.

The previous best (#6) used an options scanner with a least-bad box floor
before the index fallback.  That floor only considered boxes.  This version
keeps the same optimistic-edge scanner but widens the floor to every
*costed, size-checked, worst-case bounded* option package on the surface:

1. long and short boxes (fixed payoff)
2. long and short call/put vertical spreads (bounded by the strike width)
3. single long calls and puts (bounded by the premium paid)

Every candidate computes a guaranteed worst-case P&L after entry and
settlement commissions.  It is then ranked by that P&L divided by the
number of sessions to expiry, so the strategy can choose the cheapest way to
stay exposed when no arbitrage is present.  The index fallback remains only
as a last resort, with a deliberately cold schedule, and is almost always
pre-empted by a cheaper options floor.
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
        min_floor_horizon: int = 1,
        entry_bar: int = 4,
        force_bar: int = 10,
        daily_bias_min: float = 0.0,
        fallback_stop: float = 2.0,
        fallback_target: float = 2.0,
        fallback_skip_every: int = 5,
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
        self.force_bar = max(self.entry_bar, force_bar)
        self.daily_bias_min = daily_bias_min
        self.fallback_stop = fallback_stop
        self.fallback_target = fallback_target
        self.fallback_skip_every = max(1, fallback_skip_every)
        self.fallback_skip_rem = fallback_skip_rem % self.fallback_skip_every

        # Fee drag for each package type, including settlement fills.
        self.box_fee = 8.0 * self.commission * self.size
        self.vertical_fee = 4.0 * self.commission * self.size
        self.single_fee = 2.0 * self.commission * self.size

        self.underlying = "SPX"
        self._session = None
        self._bars_since_session = 0
        self._fallback_done_this_session = False
        self._packages_this_session = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        # The index fallback leans on the previous completed session.
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

        # Already carrying an option package or an index position.
        if ctx.assets_held():
            return

        # Try the quoted surface before the index fallback has a chance to
        # force an underlying trade.
        if (
            self._packages_this_session < self.max_packages_per_session
            and (
                self._bars_since_session == 1
                or self._bars_since_session % self.scan_every == 0
            )
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

                    floor = self._scan_floor(ctx, by_expiry)
                    if floor is not None:
                        self._place_trade(ctx, floor)
                        self._packages_this_session += 1
                        return

        # Only used if the surface gave us nothing acceptable at all.
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

            strike_map = by_expiry.setdefault(q.expiry_session, {})
            side = "call" if q.is_call else "put"
            strike_map.setdefault(q.strike, {})[side] = q
        return by_expiry

    # --- Edge scanner --------------------------------------------------------
    def _scan_edges(self, ctx, by_expiry):
        best = None
        best_pnl = -1e18

        for expiry, strike_map in by_expiry.items():
            call_by_strike = {
                k: legs["call"] for k, legs in strike_map.items() if "call" in legs
            }
            put_by_strike = {
                k: legs["put"] for k, legs in strike_map.items() if "put" in legs
            }
            both_strikes = sorted(set(call_by_strike) & set(put_by_strike))
            call_strikes = sorted(call_by_strike)
            put_strikes = sorted(put_by_strike)

            # Box spreads: model-free fixed payoff.
            for i, k1 in enumerate(both_strikes):
                c1 = call_by_strike[k1]
                p1 = put_by_strike[k1]
                for k2 in both_strikes[i + 1:]:
                    width = k2 - k1
                    if width < self.min_width or width > self.max_width:
                        continue

                    c2 = call_by_strike[k2]
                    p2 = put_by_strike[k2]

                    # Long box.
                    if (
                        c1.ask > 0.0
                        and c1.ask_size >= self.size
                        and p1.bid > 0.0
                        and p1.bid_size >= self.size
                        and c2.bid > 0.0
                        and c2.bid_size >= self.size
                        and p2.ask > 0.0
                        and p2.ask_size >= self.size
                    ):
                        cost = c1.ask - p1.bid - c2.bid + p2.ask
                        pnl = width - cost - self.box_fee
                        if pnl > best_pnl:
                            best_pnl = pnl
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

                    # Short box.
                    if (
                        c1.bid > 0.0
                        and c1.bid_size >= self.size
                        and p1.ask > 0.0
                        and p1.ask_size >= self.size
                        and c2.ask > 0.0
                        and c2.ask_size >= self.size
                        and p2.bid > 0.0
                        and p2.bid_size >= self.size
                    ):
                        credit = c1.bid - p1.ask - c2.ask + p2.bid
                        pnl = credit - width - self.box_fee
                        if pnl > best_pnl:
                            best_pnl = pnl
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

            # Call vertical spreads.
            for i, k1 in enumerate(call_strikes):
                c1 = call_by_strike[k1]
                for k2 in call_strikes[i + 1:]:
                    width = k2 - k1
                    if width < self.min_width or width > self.max_width:
                        continue
                    c2 = call_by_strike[k2]

                    # Long call vertical: buy K1, sell K2.
                    if (
                        c1.ask > 0.0
                        and c1.ask_size >= self.size
                        and c2.bid > 0.0
                        and c2.bid_size >= self.size
                    ):
                        debit = c1.ask - c2.bid
                        pnl = -debit - self.vertical_fee
                        if pnl > best_pnl:
                            best_pnl = pnl
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

                    # Short call vertical: sell K1, buy K2.
                    if (
                        c1.bid > 0.0
                        and c1.bid_size >= self.size
                        and c2.ask > 0.0
                        and c2.ask_size >= self.size
                    ):
                        credit = c1.bid - c2.ask
                        pnl = credit - width - self.vertical_fee
                        if pnl > best_pnl:
                            best_pnl = pnl
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

            # Put vertical spreads.
            for i, k1 in enumerate(put_strikes):
                p1 = put_by_strike[k1]
                for k2 in put_strikes[i + 1:]:
                    width = k2 - k1
                    if width < self.min_width or width > self.max_width:
                        continue
                    p2 = put_by_strike[k2]

                    # Long put vertical: buy K2, sell K1.
                    if (
                        p2.ask > 0.0
                        and p2.ask_size >= self.size
                        and p1.bid > 0.0
                        and p1.bid_size >= self.size
                    ):
                        debit = p2.ask - p1.bid
                        pnl = -debit - self.vertical_fee
                        if pnl > best_pnl:
                            best_pnl = pnl
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

                    # Short put vertical: sell K2, buy K1.
                    if (
                        p2.bid > 0.0
                        and p2.bid_size >= self.size
                        and p1.ask > 0.0
                        and p1.ask_size >= self.size
                    ):
                        credit = p2.bid - p1.ask
                        pnl = credit - width - self.vertical_fee
                        if pnl > best_pnl:
                            best_pnl = pnl
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

        if best is not None and best_pnl > self.min_edge:
            return best
        return None

    # --- Least-bad floor -----------------------------------------------------
    def _scan_floor(self, ctx, by_expiry):
        state = {"best": None, "score": -1e18}

        for expiry, strike_map in by_expiry.items():
            horizon = expiry - ctx.session
            if horizon < self.min_floor_horizon:
                continue

            def consider(kind, pnl, legs, **meta):
                if pnl < self.floor_min_net:
                    return
                score = pnl / float(horizon)
                if score < self.floor_min_score:
                    return
                if score > state["score"]:
                    state["score"] = score
                    state["best"] = {
                        "kind": kind,
                        "expiry": expiry,
                        "pnl": pnl,
                        "score": score,
                        "legs": legs,
                        **meta,
                    }

            call_by_strike = {
                k: legs["call"] for k, legs in strike_map.items() if "call" in legs
            }
            put_by_strike = {
                k: legs["put"] for k, legs in strike_map.items() if "put" in legs
            }

            # Single long options: worst case is the option expiring worthless.
            for k, q in call_by_strike.items():
                if q.ask > 0.0 and q.ask_size >= self.size:
                    pnl = -q.ask - self.single_fee
                    consider("long-call", pnl, [(q, "buy", self.size)], k1=k)

            for k, q in put_by_strike.items():
                if q.ask > 0.0 and q.ask_size >= self.size:
                    pnl = -q.ask - self.single_fee
                    consider("long-put", pnl, [(q, "buy", self.size)], k1=k)

            both_strikes = sorted(set(call_by_strike) & set(put_by_strike))
            call_strikes = sorted(call_by_strike)
            put_strikes = sorted(put_by_strike)

            # Boxes.
            for i, k1 in enumerate(both_strikes):
                c1 = call_by_strike[k1]
                p1 = put_by_strike[k1]
                for k2 in both_strikes[i + 1:]:
                    width = k2 - k1
                    if width < self.min_width or width > self.max_width:
                        continue
                    c2 = call_by_strike[k2]
                    p2 = put_by_strike[k2]

                    if (
                        c1.ask > 0.0
                        and c1.ask_size >= self.size
                        and p1.bid > 0.0
                        and p1.bid_size >= self.size
                        and c2.bid > 0.0
                        and c2.bid_size >= self.size
                        and p2.ask > 0.0
                        and p2.ask_size >= self.size
                    ):
                        cost = c1.ask - p1.bid - c2.bid + p2.ask
                        pnl = width - cost - self.box_fee
                        consider(
                            "box-long",
                            pnl,
                            [
                                (c1, "buy", self.size),
                                (p1, "sell", self.size),
                                (c2, "sell", self.size),
                                (p2, "buy", self.size),
                            ],
                            k1=k1,
                            k2=k2,
                        )

                    if (
                        c1.bid > 0.0
                        and c1.bid_size >= self.size
                        and p1.ask > 0.0
                        and p1.ask_size >= self.size
                        and c2.ask > 0.0
                        and c2.ask_size >= self.size
                        and p2.bid > 0.0
                        and p2.bid_size >= self.size
                    ):
                        credit = c1.bid - p1.ask - c2.ask + p2.bid
                        pnl = credit - width - self.box_fee
                        consider(
                            "box-short",
                            pnl,
                            [
                                (c1, "sell", self.size),
                                (p1, "buy", self.size),
                                (c2, "buy", self.size),
                                (p2, "sell", self.size),
                            ],
                            k1=k1,
                            k2=k2,
                        )

            # Call verticals.
            for i, k1 in enumerate(call_strikes):
                c1 = call_by_strike[k1]
                for k2 in call_strikes[i + 1:]:
                    width = k2 - k1
                    if width < self.min_width or width > self.max_width:
                        continue
                    c2 = call_by_strike[k2]

                    if (
                        c1.ask > 0.0
                        and c1.ask_size >= self.size
                        and c2.bid > 0.0
                        and c2.bid_size >= self.size
                    ):
                        debit = c1.ask - c2.bid
                        pnl = -debit - self.vertical_fee
                        consider(
                            "call-vertical-long",
                            pnl,
                            [(c1, "buy", self.size), (c2, "sell", self.size)],
                            k1=k1,
                            k2=k2,
                        )

                    if (
                        c1.bid > 0.0
                        and c1.bid_size >= self.size
                        and c2.ask > 0.0
                        and c2.ask_size >= self.size
                    ):
                        credit = c1.bid - c2.ask
                        pnl = credit - width - self.vertical_fee
                        consider(
                            "call-vertical-short",
                            pnl,
                            [(c1, "sell", self.size), (c2, "buy", self.size)],
                            k1=k1,
                            k2=k2,
                        )

            # Put verticals.
            for i, k1 in enumerate(put_strikes):
                p1 = put_by_strike[k1]
                for k2 in put_strikes[i + 1:]:
                    width = k2 - k1
                    if width < self.min_width or width > self.max_width:
                        continue
                    p2 = put_by_strike[k2]

                    if (
                        p2.ask > 0.0
                        and p2.ask_size >= self.size
                        and p1.bid > 0.0
                        and p1.bid_size >= self.size
                    ):
                        debit = p2.ask - p1.bid
                        pnl = -debit - self.vertical_fee
                        consider(
                            "put-vertical-long",
                            pnl,
                            [(p2, "buy", self.size), (p1, "sell", self.size)],
                            k1=k1,
                            k2=k2,
                        )

                    if (
                        p2.bid > 0.0
                        and p2.bid_size >= self.size
                        and p1.ask > 0.0
                        and p1.ask_size >= self.size
                    ):
                        credit = p2.bid - p1.ask
                        pnl = credit - width - self.vertical_fee
                        consider(
                            "put-vertical-short",
                            pnl,
                            [(p2, "sell", self.size), (p1, "buy", self.size)],
                            k1=k1,
                            k2=k2,
                        )

        return state["best"]

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
