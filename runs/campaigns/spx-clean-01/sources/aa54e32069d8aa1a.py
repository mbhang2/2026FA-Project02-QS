"""A costed box-spread scanner on the quoted options surface.

The idea is the model-free relationship behind any box: for two strikes K1<K2
on the same expiry, the long-box mid price

    C(K1) - P(K1) - C(K2) + P(K2)

must equal (K2-K1) * df, where df is the expiry's discount factor. We never
assume a carry rate -- we infer df from the cross-section of nearby boxes, using
the median of all sampled strike pairs. A pair whose *traded* cost (using the
observed bid/ask touch for the legs we would actually hit) implies a df far from
that median is a relative mispricing.

The strategy submits a four-legged market-order box only when the edge after
four entry fills, four settlement fills and a small safety margin is positive.
If no violation survives the spread and commissions, the strategy reports and
takes nothing. Positions are carried to expiry, where the box pays
deterministically in cash, so the capture does not rely on closing the legs.
"""

from __future__ import annotations

from statistics import median


class Strategy:
    def __init__(self, interval: str = "5m", size: int = 1,
                 min_width: float = 25.0, max_width: float = 4000.0,
                 max_pairs_per_strike: int = 8, min_edge: float = 0.5,
                 fee_per_box: float = 0.8, min_time_left: float = 300.0):
        self.interval = interval
        self.size = size
        self.min_width = min_width
        self.max_width = max_width
        self.max_pairs_per_strike = max_pairs_per_strike
        self.min_edge = min_edge
        self.fee_per_box = fee_per_box * size  # four legs, entry and settlement
        self.min_time_left_ns = int(min_time_left * 1_000_000_000)
        self.underlying = "SPX"

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        # The dataset name is e.g. "spx-options:SPX" on the options runs.
        self.underlying = ctx.dataset_name.split(":")[-1] or "SPX"

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        if ctx.assets_held() or ctx.working_order_ids:
            # One box at a time; carry it to expiry.
            return
        if ctx.session_end - ctx.ts < self.min_time_left_ns:
            # Leave room for all four legs to fill before the session close.
            return

        quotes = ctx.options()
        if not quotes:
            return

        best = self._find_best_box(ctx, quotes)
        if best is None:
            return
        self._place_box(ctx, best)

    def _find_best_box(self, ctx, quotes):
        """Return the best box trade as a tuple, or None if nothing clears costs."""
        by_expiry: dict[int, dict[float, dict[str, object]]] = {}
        for q in quotes:
            if q.expiry_session <= ctx.session:
                continue  # never trade a contract expiring this session
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
            if len(strikes) < 3:
                continue

            pair_records = []
            mid_dfs = []

            # Keep the scan roughly linear in strikes, not O(N^2). Each strike
            # is paired with a bounded number of following strikes that satisfy
            # the width limits; that is enough for both a robust median and
            # local mispricing detection.
            for i, k1 in enumerate(strikes):
                c1 = strike_map[k1].get("call")
                p1 = strike_map[k1].get("put")
                if c1 is None or p1 is None:
                    continue
                upper = min(len(strikes), i + 1 + self.max_pairs_per_strike)
                for j in range(i + 1, upper):
                    k2 = strikes[j]
                    width = k2 - k1
                    if width < self.min_width or width > self.max_width:
                        continue
                    c2 = strike_map[k2].get("call")
                    p2 = strike_map[k2].get("put")
                    if c2 is None or p2 is None:
                        continue

                    mid_box = c1.mid - p1.mid - c2.mid + p2.mid
                    # mid_box/width is this box's implied discount factor.
                    mid_dfs.append(mid_box / width)
                    pair_records.append((k1, k2, width, c1, p1, c2, p2))

            if len(mid_dfs) < 5:
                continue

            fair_df = median(mid_dfs)

            for k1, k2, width, c1, p1, c2, p2 in pair_records:
                # Long box: buy C1, sell P1, sell C2, buy P2.
                if self._long_legs_ok(c1, p1, c2, p2):
                    long_cost = c1.ask - p1.bid - c2.bid + p2.ask
                    long_df = long_cost / width
                    net = (fair_df - long_df) * width - self.fee_per_box
                    if net > self.min_edge and net > best_net:
                        best_net = net
                        best = ("long", expiry, k1, k2, c1, p1, c2, p2)

                # Short box: sell C1, buy P1, buy C2, sell P2.
                if self._short_legs_ok(c1, p1, c2, p2):
                    short_credit = c1.bid - p1.ask - c2.ask + p2.bid
                    short_df = short_credit / width
                    net = (short_df - fair_df) * width - self.fee_per_box
                    if net > self.min_edge and net > best_net:
                        best_net = net
                        best = ("short", expiry, k1, k2, c1, p1, c2, p2)

        return best

    def _long_legs_ok(self, c1, p1, c2, p2) -> bool:
        return (
            c1.ask_size >= self.size and
            p1.bid_size >= self.size and
            c2.bid_size >= self.size and
            p2.ask_size >= self.size
        )

    def _short_legs_ok(self, c1, p1, c2, p2) -> bool:
        return (
            c1.bid_size >= self.size and
            p1.ask_size >= self.size and
            c2.ask_size >= self.size and
            p2.bid_size >= self.size
        )

    def _asset_name(self, quote) -> str:
        strike = quote.strike
        if float(strike).is_integer():
            strike = int(strike)
        return f"{self.underlying}-{quote.expiry_session}-{'C' if quote.is_call else 'P'}-{strike}"

    def _place_box(self, ctx, trade) -> None:
        side, expiry, k1, k2, c1, p1, c2, p2 = trade
        tag = f"box-{side}-{expiry}-{k1:g}-{k2:g}"

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
        # Entry legs fill asynchronously; we simply hold until expiry.
        pass

    def on_lifecycle(self, ctx) -> None:
        # No stale ids are kept; box positions are held to settlement.
        pass
