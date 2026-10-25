"""Reference box-arbitrage detector, for timing the options profiles.

NOT a campaign entrant. This is the harness's own answer to the question the
options campaign asks, written so that the wall clock, the fill path and the
settlement path can all be measured before any agent is asked to do it. A
positive control for the machinery, in the same spirit as the planted-signal
ladder: if the loop cannot score THIS, the loop is broken, not the proposer.

Put-call parity says C - P = D(F - K) at every strike of one expiry. Where it
does not hold by more than the spread, the synthetic underlying is cheap on one
strike and dear on another, and buying the cheap one against the dear one is a
box: four option legs whose payoff at expiry is exactly the difference between
the strikes. It is held to settlement because that is the only place the edge
is paid -- sold back, it returns the spread and nothing else.
"""

from src.core.data.options import option_asset_of

UNDERLYING = "SPX"
MIN_EDGE = 1.0          # index points, after the spread is already inside it
MAX_OPEN = 6            # boxes carried at once


class Strategy:
    def setup(self, ctx):
        ctx.subscribe("5m")
        self.open_boxes = 0
        self.seen = 0

    def on_bars(self, ctx):
        quotes = ctx.options()
        if not quotes:
            return
        self.seen += 1
        if self.open_boxes >= MAX_OPEN:
            return

        by_expiry: dict = {}
        for q in quotes:
            by_expiry.setdefault(q.expiry_session, {}).setdefault(
                q.strike, {})[q.is_call] = q

        best = None
        for expiry, strikes in by_expiry.items():
            pairs = {k: d for k, d in strikes.items() if len(d) == 2}
            # The synthetic long costs ask(C) - bid(P) and is worth D(F - K);
            # D and F are not observable, but they are the SAME for two strikes
            # of one expiry, so the difference between two synthetics is
            # D(K2 - K1) and the unknowns cancel. That is the whole reason a
            # box can be checked without knowing the discount curve.
            for k1, a in pairs.items():
                for k2, b in pairs.items():
                    if k2 <= k1:
                        continue
                    if min(a[True].ask_size, a[False].bid_size,
                           b[True].bid_size, b[False].ask_size) < 1:
                        continue
                    cost = ((a[True].ask - a[False].bid)
                            - (b[True].bid - b[False].ask))
                    edge = (k2 - k1) - cost
                    if edge > MIN_EDGE and (best is None or edge > best[0]):
                        best = (edge, a, b)

        if best is None:
            return
        _edge, lo, hi = best
        for q, buy in ((lo[True], True), (lo[False], False),
                       (hi[True], False), (hi[False], True)):
            send = ctx.buy if buy else ctx.sell
            send(1, asset=option_asset_of(UNDERLYING, q), tif="IOC", tag="box")
        self.open_boxes += 1

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
