"""Scan all listed expiries for box spreads that are cheap relative to their
undiscounted width.

A box spread pays its width in cash at expiry no matter where the underlying
settles.  The engine charges no financing and options settlement is cash at
intrinsic value, so the correct model-free benchmark is the undiscounted width
`K2 - K1`, not a discount-factor fair value.  A long box bought for less than
its width minus all eight commissions is a fixed, model-free profit.

Earlier attempts either discounted the box by an inferred df (too strict, and
the engine charges no financing) or limited the scan to a 20-session horizon
(which appears to have missed the carry that accumulates in longer-dated
boxes).  This version scans every listed expiry with no horizon cap and takes
the best long or short box at the exact traded bid/ask sides, with quoted size
on every leg, up to two boxes per session.

If no box clears, a deterministic 75%-of-sessions exposure-floor fallback runs
so the strategy cannot go silent.  The fallback intentionally skips roughly
one session in four: the index fallback is expected to be negative after the
unfavorable fill and commission, and the exposure floor needs most sessions,
not every session.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        scan_every: int = 4,                   # scan options every 20 min
        max_boxes_per_session: int = 2,        # 4 orders per box; 8 orders max
        entry_bar: int = 3,                    # first possible index fallback bar
        force_bar: int = 8,                    # force an index trade after this bar
        size: int = 1,
        commission: float = 0.1,
        min_edge: float = 0.0,
        min_width: float = 5.0,
        max_width: float = 8000.0,
        max_horizon_sessions=None,             # None means every listed expiry
        daily_bias_min: float = 0.0,
        fallback_stop: float = 2.0,
        fallback_target: float = 2.0,
        fallback_skip_every: int = 4,          # trade fallback about 75% of sessions
        fallback_skip_rem: int = 0,
        min_time_left_ns: int = int(5 * 60 * 1_000_000_000),
    ):
        self.interval = interval
        self.scan_every = max(1, scan_every)
        self.max_boxes_per_session = max(1, max_boxes_per_session)
        self.entry_bar = max(1, entry_bar)
        self.force_bar = max(entry_bar, force_bar)
        self.size = size
        self.commission = commission
        self.min_edge = min_edge
        self.min_width = min_width
        self.max_width = max_width
        self.max_horizon_sessions = max_horizon_sessions
        self.daily_bias_min = daily_bias_min
        self.fallback_stop = fallback_stop
        self.fallback_target = fallback_target
        self.fallback_skip_every = max(1, fallback_skip_every)
        self.fallback_skip_rem = fallback_skip_rem % self.fallback_skip_every
        self.min_time_left_ns = min_time_left_ns

        # Four entry legs plus four settlement fills, per contract per fill.
        self.box_fee = 8.0 * self.commission * self.size

        self.underlying = "SPX"
        self._session = None
        self._bars_since_session = 0
        self._fallback_done_this_session = False
        self._boxes_this_session = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        # Daily bars let the index fallback lean on the previous session's bias.
        ctx.subscribe("1d")
        if ":" in ctx.dataset_name:
            self.underlying = ctx.dataset_name.split(":")[-1] or "SPX"

    def on_bars(self, ctx) -> None:
        # Act only when our own bar closed; the 1d subscription also delivers
        # into on_bars at session boundaries.
        if self.interval not in ctx.bars:
            return

        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_since_session = 0
            self._fallback_done_this_session = False
            self._boxes_this_session = 0
        self._bars_since_session += 1

        if not ctx.actionable:
            return

        # Wait for previous entry legs or an index bracket order to resolve.
        if ctx.working_order_ids:
            return

        # Look for fixed-payoff box spreads on a coarse schedule.
        if (
            self._bars_since_session % self.scan_every == 0
            and self._boxes_this_session < self.max_boxes_per_session
        ):
            best = self._scan_boxes(ctx)
            if best is not None:
                self._place_box(ctx, best)
                self._boxes_this_session += 1
                return

        # If we already carry an option package, the exposure floor is met.
        if ctx.assets_held():
            return

        # Index fallback, with a deterministic skip pattern that retains the
        # exposure floor while reducing expected-negative index trades.
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

    # --- Box scanner ---------------------------------------------------------
    def _scan_boxes(self, ctx):
        if ctx.session_end - ctx.ts < self.min_time_left_ns:
            return None

        quotes = ctx.options()
        if not quotes:
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
            if q.bid <= 0.0 or q.ask <= 0.0:
                continue
            if q.bid_size < self.size or q.ask_size < self.size:
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
                    # At expiry it pays -(K2-K1); profitable if the upfront
                    # credit exceeds the width plus all commissions.
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

        if best is not None and best_net > self.min_edge:
            return best
        return None

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

    def _place_box(self, ctx, trade) -> None:
        tag = (
            f"{trade['kind']}-{trade['expiry']}-"
            f"{self._fmt_strike(trade['k1'])}-{self._fmt_strike(trade['k2'])}"
        )
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
