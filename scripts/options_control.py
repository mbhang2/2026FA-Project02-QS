#!/usr/bin/env python3
"""The harness's own answer to the options-arbitrage question. A CONTROL.

    cp scripts/options_control.py src/strategies/current.py
    python run.py --profile spx-options-train  --human --purpose check
    python run.py --profile spx-clean-train    --human --purpose check

**Deliberately NOT in `tests/examples/` and NOT cited by
`docs/agent/strategy-api.md`.** Section 9 of that document is pasted verbatim
into every proposal prompt, so a worked box-arbitrage detector sitting in it
would hand the proposer the answer and the campaign would measure copying
rather than discovery. It lives here, where a human can run it and no prompt
can reach it.

What it is for is the same thing the planted-signal ladder is for: establishing
that the question is answerable before asking anyone to answer it. Measured
2026-10-25, 105 train sessions, `unfavorable` fills, 0.10 points per contract:

    surface   trades   net        sortino   significance   verdict
    planted    5,538   +12,665    +0.3170   +3.25          accepted
    clean          0        0.00  -inf      -inf           rejected

Zero trades on the clean surface is the number that matters. The surface is
arbitrage-free by construction and the detector agrees across 105 sessions, so
an accept on the planted arm is the plant and not the method.

It also carries the mistake that makes this dataset worth having -- see the
comment on the discounted payoff. The first version compared the box's cost
against the UNDISCOUNTED strike width, found an edge at every width, and lost
10,409 points in 1,174 trades on a surface containing exactly one violation.
That is the trap, and it is the one a proposer is most likely to fall into.
"""

from src.core.data.options import option_asset_of

UNDERLYING = "SPX"
MIN_EDGE = 1.0          # index points, after the spread is already inside it
MAX_OPEN = 10_000       # effectively uncapped: take every one


def _discount_of(pairs: dict) -> float | None:
    """The expiry's discount factor, read off its own quotes.

    Parity says `C - P = D(F - K)` at every strike of one expiry, so the mid
    synthetic is a straight line in K with slope `-D`. Least squares over the
    whole ladder recovers it. Nothing hands a strategy the curve -- `D` and `F`
    are not fields on a quote, and inventing a rate would be fitting a number
    the data can supply.

    Mids rather than touches: the slope is wanted, not an executable price, and
    the spread is roughly symmetric so it cancels. The executable check happens
    afterwards, against the touch.
    """
    ks = sorted(pairs)
    n = len(ks)
    sx = sum(ks)
    sy = 0.0
    sxy = 0.0
    sxx = 0.0
    for k in ks:
        d = pairs[k]
        y = d[True].mid - d[False].mid
        sy += y
        sxy += k*y
        sxx += k*k
    denom = n*sxx - sx*sx
    if denom <= 0.0:
        return None
    slope = (n*sxy - sx*sy)/denom
    disc = -slope
    # A discount factor outside (0, 1] means the fit found something that is
    # not a discount curve, and acting on it would be acting on noise.
    return disc if 0.0 < disc <= 1.0 else None


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
            if len(pairs) < 4:
                continue
            disc = _discount_of(pairs)
            if disc is None:
                continue
            for k1, a in pairs.items():
                for k2, b in pairs.items():
                    if k2 <= k1:
                        continue
                    if min(a[True].ask_size, a[False].bid_size,
                           b[True].bid_size, b[False].ask_size) < 1:
                        continue
                    cost = ((a[True].ask - a[False].bid)
                            - (b[True].bid - b[False].ask))
                    # **DISCOUNTED payoff.** The box pays K2 - K1 at expiry and
                    # is paid for today, so comparing the cost against the
                    # undiscounted width is not conservative, it is wrong -- and
                    # on this surface it is catastrophically wrong. SPX strikes
                    # run from about 5,850 to 9,750, so a wide box spans ~3,900
                    # points; at four months and 4.4% the discount is 0.28% of
                    # that, which is ELEVEN POINTS of pure carry masquerading as
                    # edge at every width. The first version of this file used
                    # the undiscounted width, found an "arbitrage" in every
                    # corner of the ladder, and lost 10,409 points over 1,174
                    # trades on a surface that contains one real violation.
                    edge = disc*(k2 - k1) - cost
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
