"""Scan box spreads on the quoted options surface; fall back to one index momentum trade per session.

The scanner infers an expiry's discount factor from the cross-section of
C_mid - P_mid versus strike using a Theil-Sen slope. Any strike pair whose
*traded* box price is cheap or rich relative to that fair box, after eight
per-contract commissions, is a candidate. If no box clears costs, a single
bracketed index trade per session guarantees the required exposure rather than
letting the run be unscored for never being in the market.

Box legs are market orders on quoted option sizes, so the computed ask/bid
touch is the real cost. Positions are held to expiry, where a long box pays
K2-K1 in cash, so the capture does not depend on closing the legs.
"""

from __future__ import annotations

from statistics import median


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        scan_every: int = 12,          # scan options once per hour (12 x 5m)
        fallback_bar: int = 3,         # enter index fallback on 3rd bar of session
        size: int = 1,
        min_width: float = 5.0,
        max_width: float = 4000.0,
        min_edge: float = 0.0,
        commission: float = 0.1,
        fallback_stop: float = 2.0,
        fallback_target: float = 2.0,
        max_fit_points: int = 30,
        max_pairs_per_expiry: int = 20000,
    ):
        self.interval = interval
        self.scan_every = max(1, scan_every)
        self.fallback_bar = max(1, fallback_bar)
        self.size = size
        self.min_width = min_width
        self.max_width = max_width
        self.min_edge = min_edge
        self.commission = commission
        self.fallback_stop = fallback_stop
        self.fallback_target = fallback_target
        self.max_fit_points = max_fit_points
        self.max_pairs_per_expiry = max_pairs_per_expiry

        # Four legs entered, four settled, commission per contract per fill.
        self.box_fee = 8.0 * self.commission * self.size

        self.underlying = "SPX"
        self._session = None
        self._bars_since_session = 0
        self._fallback_done_this_session = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        # The dataset name is e.g. "spx-options:SPX".
        if ":" in ctx.dataset_name:
            self.underlying = ctx.dataset_name.split(":")[-1] or "SPX"

    def on_bars(self, ctx) -> None:
        # Our subscription is the only one, but in a multi-timeframe run this
        # callback could fire for another bar. We act only when our bar closes.
        if self.interval not in ctx.bars:
            return

        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_since_session = 0
            self._fallback_done_this_session = False
        self._bars_since_session += 1

        if not ctx.actionable:
            return

        # One position/set of orders at a time. Box positions can be held for
        # sessions; while they are, skip all other trading.
        if ctx.assets_held() or ctx.working_order_ids:
            return

        # Scan the options surface on a coarse schedule.
        if self._bars_since_session % self.scan_every == 0:
            best = self._find_best_box(ctx)
            if best is not None:
                self._place_box(ctx, best)
                return

        # Fallback: a single bracketed index entry per session, so the strategy
        # is exposed even when there is no costed option mispricing.
        if not self._fallback_done_this_session and self._bars_since_session >= self.fallback_bar:
            history = ctx.history(self.interval)
            if not len(history):
                return
            last = history[-1]
            if last.close == last.open:
                return
            place = ctx.buy if last.close > last.open else ctx.sell
            oid = place(
                size=self.size,
                stop_offset=self.fallback_stop,
                target_offset=self.fallback_target,
                tag="index-fallback",
            )
            if oid is not None:
                self._fallback_done_this_session = True

    # --- Options box scanner -------------------------------------------------
    def _find_best_box(self, ctx):
        quotes = ctx.options()
        if not quotes:
            return None

        by_expiry: dict[int, dict[float, dict[str, object]]] = {}
        for q in quotes:
            if q.expiry_session <= ctx.session:
                continue  # never trade a contract that expires this session
            if q.bid <= 0.0 or q.ask <= 0.0:
                continue
            if q.bid_size < self.size or q.ask_size < self.size:
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

            fair_df = self._fair_df_for_expiry(strikes, strike_map)
            if fair_df is None:
                continue

            pair_count = 0
            for i, k1 in enumerate(strikes):
                c1 = strike_map[k1]["call"]
                p1 = strike_map[k1]["put"]
                for j in range(i + 1, len(strikes)):
                    k2 = strikes[j]
                    width = k2 - k1
                    if width < self.min_width or width > self.max_width:
                        continue
                    pair_count += 1
                    if pair_count > self.max_pairs_per_expiry:
                        break

                    c2 = strike_map[k2]["call"]
                    p2 = strike_map[k2]["put"]

                    # Long box: buy C1, sell P1, sell C2, buy P2. Payoff K2-K1.
                    if self._long_legs_ok(c1, p1, c2, p2):
                        long_cost = c1.ask - p1.bid - c2.bid + p2.ask
                        edge = (fair_df * width - long_cost) - self.box_fee
                        if edge > self.min_edge and edge > best_edge:
                            best_edge = edge
                            best = ("long", expiry, k1, k2, c1, p1, c2, p2)

                    # Short box: sell C1, buy P1, buy C2, sell P2.
                    if self._short_legs_ok(c1, p1, c2, p2):
                        short_credit = c1.bid - p1.ask - c2.ask + p2.bid
                        edge = (short_credit - fair_df * width) - self.box_fee
                        if edge > self.min_edge and edge > best_edge:
                            best_edge = edge
                            best = ("short", expiry, k1, k2, c1, p1, c2, p2)

                if pair_count > self.max_pairs_per_expiry:
                    break

        return best

    def _fair_df_for_expiry(self, strikes, strike_map) -> float | None:
        """Bootstrap a discount factor from the cross-section.

        C_mid - P_mid = df * (F - K), so the slope of C_mid-P_mid against the
        strike is -df. The median pairwise slope is robust to a single bad
        quote. We sample to a bounded number of points when the ladder is long.
        """
        points = []
        for k in strikes:
            c = strike_map[k]["call"]
            p = strike_map[k]["put"]
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

    def _long_legs_ok(self, c1, p1, c2, p2) -> bool:
        return (
            c1.ask_size >= self.size
            and p1.bid_size >= self.size
            and c2.bid_size >= self.size
            and p2.ask_size >= self.size
        )

    def _short_legs_ok(self, c1, p1, c2, p2) -> bool:
        return (
            c1.bid_size >= self.size
            and p1.ask_size >= self.size
            and c2.ask_size >= self.size
            and p2.bid_size >= self.size
        )

    def _fmt_strike(self, strike) -> str:
        f = float(strike)
        if f.is_integer():
            return str(int(f))
        s = f"{f:.2f}".rstrip("0").rstrip(".")
        return s or "0"

    def _asset_name(self, quote) -> str:
        return (
            f"{self.underlying}-{quote.expiry_session}-"
            f"{'C' if quote.is_call else 'P'}-{self._fmt_strike(quote.strike)}"
        )

    def _place_box(self, ctx, trade) -> None:
        side, expiry, k1, k2, c1, p1, c2, p2 = trade
        tag = f"box-{side}-{expiry}-{self._fmt_strike(k1)}-{self._fmt_strike(k2)}"

        if side == "long":
            ctx.buy(size=self.size, asset=self._asset_name(c1), tag=tag)
            ctx.sell(size=self.size, asset=self._asset_name(p1), tag=tag)
            ctx.sell(size=self.size, asset=self._asset_name(c2), tag=tag)
            ctx.buy(size=self.size, asset=self._asset_name(p2), tag=tag)
        else:
            ctx.sell(size=self.size, asset=self._asset_name(c1), tag=tag)
            ctx.buy(size=self.size, asset=self._asset_name(p1), tag=tag)
            ctx.buy(size=self.size, asset=self._asset_name(c2), tag=tag)
            ctx.sell(size=self.size, asset=self._asset_name(p2), tag=tag)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
