"""A synthetic options surface, arbitrage-free by construction, with an
optional planted arbitrage of known size.

    spec = SyntheticOptionsSpec(arb_strength=0.0)       # nothing to find
    spec = SyntheticOptionsSpec(arb_strength=0.5)       # 0.50 of locked-in edge

**The hard half is the baseline, not the plant.** A surface built by evaluating
a smooth Black-Scholes smile is NOT arbitrage-free: a steep enough smile breaks
butterfly convexity, discrete strikes break it again at the wings, and floating
point breaks parity in the last place. An agent pointed at such a surface finds
"arbitrage" everywhere, none of it planted, and the oracle is meaningless --
which would make the whole exercise unfalsifiable.

So prices here are **discounted expectations under an explicit discrete
risk-neutral density**, and every no-arbitrage condition is then a theorem
about `max(S - K, 0)` rather than a property we hope survived:

    put-call parity     C(K) - P(K) = sum_j p_j (S_j - K) = F - K, exactly
    vertical            C is non-increasing in K, P non-decreasing: monotone
    butterfly           C(K-h) - 2C(K) + C(K+h) >= 0: convex in K
    calendar            each expiry's density is the SAME process convolved
                        further, so variance only increases and the forward is
                        a martingale

The last one is why the densities are built by repeated convolution of one
kernel rather than drawn per expiry. Two independently drawn densities can
easily imply a cheaper long-dated option than a short-dated one at the same
strike, which is a calendar arbitrage nobody planted.

**Quotes, not mids.** A strategy sees `bid` and `ask`. Mid prices being
arbitrage-free is necessary and not sufficient: what matters is that no
combination is profitable when you buy at the ask and sell at the bid. A
symmetric spread around an arbitrage-free mid can only make that harder, so the
baseline is arbitrage-free in the executable sense too -- and a planted edge has
to clear the spread before it is findable, which is exactly the signal-strength
relationship the planted-momentum generator already has.

**The plant.** One (expiry, strike) pair has its call quote shifted down by
`arb_strength`. Parity is the cleanest available plant because parity is an
identity, not a model: detecting it needs no volatility assumption, no
interpolation and no fitting, so a failure to find it is a failure of the loop
rather than a disagreement about method.

**It does not stay confined to parity, and the strength decides how many routes
see it.** Measured on the default surface at `half_spread=0.05`:

    strength <= 0.10   no route      the plant is under the 2h it must clear
    0.15 - 0.30        parity only   one route, and it needs the identity
    0.40 - 0.45        + butterfly   a big enough shift breaks convexity too
    0.50 - 0.70        + calendar
    0.75 +             + vertical

An early draft of this docstring claimed the plant broke "parity and nothing
else", which the table above disproves: shifting one option far enough makes
its own strike ladder non-convex and its own expiry ladder non-monotone. That
is realistic -- a real mispricing shows up in several relationships at once --
but it means **the strength is a difficulty dial, not just a size**. Use
0.15-0.30 to ask whether the loop can find a violation of an identity, and 0.50
up to ask only whether it can find something.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field, replace

import numpy as np

NS_PER_SECOND = 1_000_000_000


@dataclass(frozen=True, slots=True)
class OptionQuote:
    """One option's consolidated best bid and offer at one instant.

    `expiry_session` is a session index, not a date: the engine counts sessions
    and `Tick.session` is already an index, so an options series that spoke in
    dates would be the one place two id spaces met. Positions are not modelled
    here -- this is a data feed.
    """

    ts: int
    session: int
    expiry_session: int
    strike: float
    is_call: bool
    bid: float
    ask: float
    # **Appended, and defaulting to zero, which means "cannot be traded".**
    # Zero is the honest default rather than a convenient one: a quote whose
    # size nobody stated is a quote you cannot hit, and the checks below refuse
    # to report an arbitrage that needs a side with no size behind it. An
    # arbitrage you cannot take is not one, and a detector that ignores size
    # reports edges nobody can capture -- which is precisely the failure a gate
    # should catch rather than reward.
    #
    # Appended at the END of the field list on purpose: the remote protocol
    # encodes a quote by `dataclasses.fields()` order and decodes by zipping
    # the same order, so adding here is wire-compatible and inserting in the
    # middle would silently shift every value by one.
    bid_size: int = 0
    ask_size: int = 0

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid + self.ask)


@dataclass(frozen=True, slots=True)
class Plant:
    """One planted violation. Several may coexist on one surface.

    `kind` names the condition broken, because which one it is decides what a
    proposer must implement to find it: parity needs an identity, butterfly
    needs convexity across a strike ladder, calendar needs monotonicity across
    an expiry ladder. A surface carrying one of each asks whether the loop
    finds the easy one and stops.
    """

    kind: str                   # "parity" | "butterfly" | "calendar"
    expiry_index: int
    strike_offset: int          # strikes from the money, signed
    strength: float

    KINDS = ("parity", "butterfly", "calendar")


@dataclass(frozen=True)
class SyntheticOptionsSpec:
    """Shaped on QQQ: a ~$500 underlying, a mixed strike ladder, a weekly-ish
    expiry ladder, and quotes on the OPRA increment grid."""

    # ---- the underlying --------------------------------------------------- #
    symbol: str = "QQQ"
    start_price: float = 500.0
    # QQQ moves ~1% a session; 5.0 on 500 is about that, in price units.
    vol_per_session: float = 5.0
    underlying_tick: float = 0.01

    # ---- the strike ladder ------------------------------------------------ #
    # Real QQQ lists $1 strikes near the money and $5 out in the wings. A
    # uniform grid hides the thing that makes wing arbitrage checks awkward:
    # a butterfly needs three EQUALLY spaced strikes, and they stop being
    # equally spaced exactly where the spacing changes.
    near_spacing: float = 1.0
    wing_spacing: float = 5.0
    near_halfwidth: float = 20.0          # +/- this far from the money at $1
    wing_halfwidth: float = 75.0          # out to this far at $5

    # ---- the expiry ladder ------------------------------------------------ #
    # Sessions, not dates: `Tick.session` is already an index and a feed that
    # spoke in dates would be the one place two id spaces met. Roughly 1d, 2d,
    # end of week, next week, two weeks, a month, two months, a quarter.
    expiries_in_sessions: tuple[int, ...] = (1, 2, 5, 10, 21, 42, 63)

    # ---- resolution ------------------------------------------------------- #
    quotes_per_session: int = 390         # one a minute over 6.5h
    quote_spacing_ns: int = 60 * NS_PER_SECOND

    # ---- the quote -------------------------------------------------------- #
    # **Spreads are not constant, and that is the realism that matters.** A
    # constant half-spread makes every wing option as tradeable as the
    # at-the-money one, which is the single most flattering thing a synthetic
    # options feed can do: in practice the wings are where apparent arbitrage
    # lives and where the spread eats it. Modelled as a floor plus a fraction
    # of premium plus a widening in the far expiries.
    min_half_spread: float = 0.01
    half_spread_frac: float = 0.02        # of premium
    far_expiry_widening: float = 0.0015   # per session of maturity, in price
    # OPRA: a penny increment under $3.00 premium and a nickel at or above it.
    penny_below: float = 3.00
    penny_increment: float = 0.01
    nickel_increment: float = 0.05

    # ---- quoted size ------------------------------------------------------- #
    size_at_the_money: int = 50
    size_decay_moneyness: float = 0.06     # sd of the log-moneyness decay
    size_maturity_decay: float = 0.04      # per session of maturity

    # ---- the plants -------------------------------------------------------- #
    plants: tuple[Plant, ...] = ()
    # Shorthand for one parity plant, kept because most campaigns want exactly
    # that and `plants=(Plant("parity", 2, 0, 0.3),)` is a lot of punctuation.
    arb_strength: float = 0.0
    arb_expiry_index: int = 2
    arb_strike_offset: int = 0

    # ---- bookkeeping ------------------------------------------------------- #
    seed: int = 0
    n_sessions: int = 120
    session_gap_ns: int = 17 * 3600 * NS_PER_SECOND
    density_step: float = 0.25
    density_halfwidth: float = 260.0      # price units either side of the forward

    def all_plants(self) -> tuple[Plant, ...]:
        if self.plants:
            if self.arb_strength:
                raise ValueError(
                    "set plants= or arb_strength=, not both: two ways of saying "
                    "what was planted is one way of disagreeing with yourself")
            return self.plants
        if self.arb_strength:
            return (Plant("parity", self.arb_expiry_index,
                          self.arb_strike_offset, self.arb_strength),)
        return ()

    def digest(self) -> str:
        parts = [f"{k}={getattr(self, k)!r}" for k in sorted(self.__dataclass_fields__)]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


class SyntheticOptionsSurface:
    """Prices every option by discounted expectation under one discrete density.

    Rates are zero, deliberately. A non-zero rate adds a discount factor to
    every leg and changes nothing about which conditions hold, while giving
    parity a term whose sign a reader has to track. The point of this dataset is
    that parity is exact; r = 0 keeps it legible.
    """

    def __init__(self, spec: SyntheticOptionsSpec | None = None) -> None:
        self.spec = spec or SyntheticOptionsSpec()
        s = self.spec
        if not s.expiries_in_sessions or min(s.expiries_in_sessions) < 1:
            raise ValueError("expiries are counted in whole sessions, at least 1")
        if sorted(s.expiries_in_sessions) != list(s.expiries_in_sessions):
            raise ValueError("expiries must be given in increasing order")
        if s.wing_halfwidth < s.near_halfwidth:
            raise ValueError("the wings must reach at least as far as the near strikes")
        for pl in s.all_plants():
            if pl.kind not in Plant.KINDS:
                raise ValueError(f"unknown plant kind {pl.kind!r}; one of {Plant.KINDS}")
            if pl.expiry_index not in range(len(s.expiries_in_sessions)):
                raise ValueError(f"plant expiry_index {pl.expiry_index} is not an expiry")
            if pl.kind == "calendar" and pl.expiry_index >= len(s.expiries_in_sessions) - 1:
                raise ValueError("a calendar plant needs a further expiry to break against")
        self.name = f"synthetic-options:{s.symbol}:plants={len(s.all_plants())}:seed={s.seed}"
        self._densities: dict[int, tuple] = {}

    # ---- the density ------------------------------------------------------ #
    def density(self, sessions_ahead: int) -> tuple:
        """`(offsets, probabilities)` for the terminal price, relative to the
        forward, `sessions_ahead` sessions out.

        **A single Gaussian per expiry, not one kernel convolved n times.**
        Gaussians are infinitely divisible, so N(0, T s^2) IS N(0, T1 s^2)
        convolved with N(0, (T-T1) s^2): building each expiry directly is both
        exactly consistent across the ladder and far cheaper than 63 passes of
        `np.convolve`, which also accumulates tail truncation. Calendar
        monotonicity still follows from one process, which is the property that
        matters -- it is just reached by arithmetic instead of by iteration.
        """
        if sessions_ahead in self._densities:
            return self._densities[sessions_ahead]
        s = self.spec
        sd = s.vol_per_session * (sessions_ahead ** 0.5)
        n = int(s.density_halfwidth / s.density_step)
        offsets = np.arange(-n, n + 1) * s.density_step
        d = np.exp(-0.5 * (offsets / sd) ** 2)
        total = d.sum()
        if total <= 0:
            raise ValueError("density collapsed; widen density_halfwidth")
        out = (offsets, d / total)
        self._densities[sessions_ahead] = out
        return out

    # ---- pricing ---------------------------------------------------------- #
    def price(self, forward: float, strike: float, sessions_ahead: int,
              is_call: bool) -> float:
        """Discounted expectation of the payoff. r = 0, so no discount factor.

        The scalar reference. `call_prices` is the vectorised path the feed
        uses, and `test_the_fast_path_agrees_with_the_reference` holds them
        together.
        """
        offsets, prob = self.density(sessions_ahead)
        terminal = forward + offsets
        payoff = (np.maximum(terminal - strike, 0.0) if is_call
                  else np.maximum(strike - terminal, 0.0))
        return float(np.dot(prob, payoff))

    def call_prices(self, forward: float, strikes, sessions_ahead: int) -> np.ndarray:
        """Every call on the ladder at once, as one matrix product.

        A snapshot is 882 quotes against a 2,081-point grid, and pricing them
        one at a time is ~1.8M scalar operations per minute of the walk -- which
        at 390 minutes a session is not serveable. Batching the payoff into a
        (strikes x grid) matrix makes it one `@`, which numpy does in about a
        millisecond.
        """
        offsets, prob = self.density(sessions_ahead)
        terminal = forward + offsets
        payoff = np.maximum(terminal[None, :] - np.asarray(strikes, dtype=float)[:, None], 0.0)
        return payoff @ prob

    def put_prices(self, forward: float, strikes, sessions_ahead: int) -> np.ndarray:
        """**Derived from the calls by parity, not priced independently.**

        P = C - (F - K). Doing it this way halves the work and, more
        importantly, makes parity exact *as an identity of the implementation*
        rather than as a property of two separate sums that happen to agree.
        There is then no arithmetic path by which the puts could drift from the
        calls -- a reassurance worth more here than the speed, because parity is
        the condition the cleanest plant breaks.
        """
        k = np.asarray(strikes, dtype=float)
        return self.call_prices(forward, k, sessions_ahead) - forward + k

    # ---- the ladder ------------------------------------------------------- #
    def strikes(self, forward: float) -> list[float]:
        """$1 near the money, $5 in the wings, centred on the money.

        The spacing change is deliberate and is where a butterfly check has to
        be careful: three strikes are only a butterfly when they are EQUALLY
        spaced, and at the boundary they are not.
        """
        s = self.spec
        atm = round(forward / s.near_spacing) * s.near_spacing
        out = {atm + k * s.near_spacing
               for k in range(-int(s.near_halfwidth / s.near_spacing),
                              int(s.near_halfwidth / s.near_spacing) + 1)}
        step = s.wing_spacing
        lo = atm - s.wing_halfwidth
        hi = atm + s.wing_halfwidth
        k = round(lo / step) * step
        while k <= hi:
            if abs(k - atm) > s.near_halfwidth:
                out.add(k)
            k += step
        return sorted(round(x, 6) for x in out)

    # ---- the quote -------------------------------------------------------- #
    def increment(self, mid: float) -> float:
        """OPRA: a penny under $3.00 of premium, a nickel at or above."""
        s = self.spec
        return s.penny_increment if mid < s.penny_below else s.nickel_increment

    def sizes(self, mid: float, strike: float, forward: float,
              sessions_ahead: int) -> tuple[int, int]:
        """Quoted size, largest at the money and thinning into the wings.

        A Gaussian in log-moneyness, floored at one contract, with no bid at
        all where the premium rounds to zero -- a 0.00 bid is not a thin bid,
        it is nobody bidding. Shape rather than a fit: the point is that size
        VARIES, so a detector has to ask whether an edge is takeable.
        """
        s = self.spec
        if forward <= 0 or strike <= 0:
            return 0, 0
        m = math.log(strike/forward)
        decay = math.exp(-(m/s.size_decay_moneyness)**2)
        far = 1.0/(1.0 + s.size_maturity_decay*sessions_ahead)
        n = max(1, int(round(s.size_at_the_money*decay*far)))
        return n, n

    def half_spread(self, mid: float, sessions_ahead: int) -> float:
        """A floor, plus a fraction of premium, plus a widening with maturity."""
        s = self.spec
        return (s.min_half_spread
                + s.half_spread_frac * mid
                + s.far_expiry_widening * sessions_ahead)

    def _quote(self, mid: float, sessions_ahead: int) -> tuple[float, float]:
        """**Both sides are rounded AWAY from the mid, and that is a correctness
        property rather than a convention.**

        Quotes live on a discrete grid, so they have to be rounded. Rounding to
        nearest would move a bid up or an ask down by up to half an increment,
        and either of those manufactures an edge that nothing planted -- on a
        nickel grid, up to 0.05 per leg, which is larger than some plants. Bids
        round down and asks round up, so quantisation can only ever make an
        arbitrage harder to find. The cost is that a planted edge is reduced by
        up to one increment per leg, which is why `oracle()` reports a bound
        rather than an equality.
        """
        h = self.half_spread(mid, sessions_ahead)
        inc = self.increment(mid)
        bid = max(0.0, np.floor((mid - h) / inc) * inc)
        ask = np.ceil((mid + h) / inc) * inc
        return round(float(bid), 6), round(float(ask), 6)

    # ---- the plants -------------------------------------------------------- #
    def _plant_sites(self, forward: float) -> dict:
        """(expiry_sessions, strike, is_call) -> signed shift, per plant kind.

        parity    the call is cheapened, so C - P falls below F - K.
        butterfly the middle call is dearer, so the fly costs less than zero.
        calendar  the near call is dearer, so its bid can exceed the far ask.
        """
        s = self.spec
        ks = self.strikes(forward)
        atm_i = min(range(len(ks)), key=lambda i: abs(ks[i] - forward))
        sites: dict = {}
        for pl in s.all_plants():
            expiry = s.expiries_in_sessions[pl.expiry_index]
            i = min(max(atm_i + pl.strike_offset, 0), len(ks) - 1)
            if pl.kind == "parity":
                sites[(expiry, ks[i], True)] = -pl.strength
            elif pl.kind == "butterfly":
                sites[(expiry, ks[i], True)] = +pl.strength
            elif pl.kind == "calendar":
                sites[(expiry, ks[i], True)] = +pl.strength
        return sites

    def threshold(self, plant: Plant, forward: float) -> dict:
        """The strength at which `plant`'s NAMED condition first fires.

        **Spreads are not the whole cost, and assuming they were made the first
        oracle wrong in a way the checker caught.** A calendar plant has to
        clear the calendar premium -- a further expiry is genuinely worth more
        than a nearer one at the same strike -- before any spread is paid, and
        a butterfly plant has to clear the convexity slack already in the
        ladder. Only parity has no intrinsic term, because parity is an
        identity: `C - P - (F - K)` is exactly zero before the plant, so the
        spread is all there is.

        A calendar plant of 0.45 at a 2-vs-5 session pair reported `findable`
        and produced no calendar violation at all, because the premium between
        those expiries is several times that.
        """
        s = self.spec
        ks = self.strikes(forward)
        atm_i = min(range(len(ks)), key=lambda i: abs(ks[i] - forward))
        i = min(max(atm_i + plant.strike_offset, 0), len(ks) - 1)
        strike = ks[i]
        expiry = s.expiries_in_sessions[plant.expiry_index]
        h = lambda k, e, c: self.half_spread(self.price(forward, k, e, c), e)  # noqa: E731

        # The mid this plant will actually be quoted from: half_spread is
        # proportional to premium, so the plant narrows its own leg's spread.
        # Latent here and caught on the SPX surface, where the effect clears
        # quantisation -- the bound was exceeded by 0.20 at a strength of 60.
        c_clean = self.price(forward, strike, expiry, True)
        c_q = max(c_clean - plant.strength, 0.0) if plant.kind == "parity" \
            else c_clean + plant.strength
        hq = self.half_spread(c_q, expiry)
        if plant.kind == "parity":
            intrinsic = 0.0
            spread = hq + h(strike, expiry, False)
            legs = 2
        elif plant.kind == "butterfly":
            lo, hi = ks[max(i - 1, 0)], ks[min(i + 1, len(ks) - 1)]
            span = hi - lo
            w = (hi - strike) / span if span > 0 else 0.5
            # how convex the ladder already is, in the same units as the plant
            intrinsic = (w * self.price(forward, lo, expiry, True)
                         + (1 - w) * self.price(forward, hi, expiry, True)
                         - self.price(forward, strike, expiry, True))
            spread = (w * h(lo, expiry, True) + (1 - w) * h(hi, expiry, True) + hq)
            legs = 3
        else:
            far = s.expiries_in_sessions[plant.expiry_index + 1]
            intrinsic = (self.price(forward, strike, far, True)
                         - self.price(forward, strike, expiry, True))
            spread = hq + h(strike, far, True)
            legs = 2
        return {"kind": plant.kind, "expiry_session": expiry, "strike": strike,
                "intrinsic_to_clear": intrinsic, "spread_to_clear": spread,
                "threshold": intrinsic + spread, "legs": legs}

    def oracle(self, forward: float | None = None) -> dict:
        """Per plant: what it must clear, and what it is worth if it does.

        `net_per_unit_upper` is an UPPER BOUND, for two reasons that are both
        real: quantisation moves each leg away from the mid by up to one
        increment, and the futures leg of a conversion is the engine's fill
        model, not this surface's to price.

        **A plant does not stay inside its own condition.** Shifting one option
        far enough makes its strike ladder non-convex and its expiry ladder
        non-monotone, so a single plant can show up in parity, vertical and
        butterfly at once -- `routes_seen` reports what is actually there by
        running the checks, which is the only honest way to say it. The
        analytic half of this method describes the plant that was asked for;
        the measured half describes the surface that resulted.
        """
        fwd = self.spec.start_price if forward is None else forward
        rows = []
        for pl in self.spec.all_plants():
            t = self.threshold(pl, fwd)
            net = pl.strength - t["threshold"]
            rows.append({**t, "planted": pl.strength,
                         "net_per_unit_upper": net, "findable": net > 0.0})
        quotes = self.quotes_at(0, 0, fwd)
        routes = {name: fn(quotes, fwd) if name == "parity" else fn(quotes)
                  for name, fn in ALL_CHECKS.items()}
        return {
            "forward": fwd,
            "plants": rows,
            "routes_seen": {k: len(v) for k, v in routes.items()},
            "best_edge_by_route": {k: (max(r["edge"] for r in v) if v else 0.0)
                                   for k, v in routes.items()},
            "note": ("net_per_unit_upper is an upper bound on the capturable edge, "
                     "on the OPTION legs only: quantisation takes up to one "
                     "increment per leg, and a conversion's futures leg belongs to "
                     "the engine's fill model. threshold = intrinsic_to_clear + "
                     "spread_to_clear, and only parity has a zero intrinsic term "
                     "because only parity is an identity. routes_seen is measured "
                     "from the quotes, not predicted, because a plant leaks into "
                     "neighbouring conditions."),
        }

    # ---- quotes ----------------------------------------------------------- #
    def quotes_at(self, ts: int, session: int, forward: float) -> list[OptionQuote]:
        """The whole surface at one instant: every strike, every expiry, both
        sides. The cross-section is the point -- an arbitrage is a statement
        about several quotes at once, so a feed delivering them one at a time
        would make the task reassembly rather than detection."""
        s = self.spec
        sites = self._plant_sites(forward)
        ks = self.strikes(forward)
        out: list[OptionQuote] = []
        for expiry in s.expiries_in_sessions:
            calls = self.call_prices(forward, ks, expiry)
            puts = self.put_prices(forward, ks, expiry)
            for i, strike in enumerate(ks):
                for is_call, mid in ((True, float(calls[i])), (False, float(puts[i]))):
                    mid += sites.get((expiry, strike, is_call), 0.0)
                    mid = max(mid, 0.0)
                    bid, ask = self._quote(mid, expiry)
                    bn, an = self.sizes(mid, strike, forward, expiry)
                    # **The bid's size is keyed off the BID, not the mid.** A
                    # quote can round to a 0.00 bid while its mid is well above
                    # zero, and the first version left size behind that bid --
                    # "0.00 x1", a price nobody is making with depth behind it.
                    if bid <= 0.0:
                        bn = 0
                    out.append(OptionQuote(
                        ts=ts, session=session, expiry_session=session + expiry,
                        strike=float(strike), is_call=is_call, bid=bid, ask=ask,
                        bid_size=bn, ask_size=an))
        return out


# --------------------------------------------------------------------------- #
# The no-arbitrage conditions, as checks rather than as comments.
# --------------------------------------------------------------------------- #
# These are what the tests assert about the baseline and what a proposer would
# have to implement to find the plant. They are written on QUOTES, not mids,
# because an arbitrage you cannot cross the spread to take is not one.
# --------------------------------------------------------------------------- #
def parity_violations(quotes: list[OptionQuote], forward: float,
                      tol: float = 1e-9) -> list[dict]:
    """C - P = F - K. Reported when the executable package beats it.

    Buying the call at its ask and selling the put at its bid costs
    `ask_c - bid_p`; parity says `F - K`. A violation is only real when the
    gap exceeds zero after the spread is already inside these two numbers.
    """
    out = []
    by: dict[tuple[int, float], dict[bool, OptionQuote]] = {}
    for q in quotes:
        by.setdefault((q.expiry_session, q.strike), {})[q.is_call] = q
    for (expiry, strike), pair in sorted(by.items()):
        c, p = pair.get(True), pair.get(False)
        if c is None or p is None:
            continue
        fair = forward - strike
        # **Each direction is only an edge if the sides it hits have size.**
        # Long via options buys the call at its ask and sells the put at its
        # bid, so it is limited by `min(c.ask_size, p.bid_size)`; a zero there
        # means the package cannot be put on at all. Reporting it anyway is the
        # failure this is here to prevent -- an arbitrage you cannot take is
        # not one, and a detector that ignores size reports edges nobody can
        # capture.
        cheap = fair - (c.ask - p.bid)
        cheap_size = min(c.ask_size, p.bid_size)
        dear = (c.bid - p.ask) - fair
        dear_size = min(c.bid_size, p.ask_size)
        if cheap_size < 1:
            cheap = -math.inf
        if dear_size < 1:
            dear = -math.inf
        edge = max(cheap, dear)
        if edge > tol:
            long_side = cheap >= dear
            out.append({"expiry_session": expiry, "strike": strike,
                        "edge": edge,
                        "size": cheap_size if long_side else dear_size,
                        "side": "long_via_options" if long_side else "short_via_options"})
    return out


def vertical_violations(quotes: list[OptionQuote], tol: float = 1e-9) -> list[dict]:
    """A call spread cannot cost less than nothing, nor more than its width."""
    out = []
    groups: dict[tuple[int, bool], list[OptionQuote]] = {}
    for q in quotes:
        groups.setdefault((q.expiry_session, q.is_call), []).append(q)
    for (expiry, is_call), qs in sorted(groups.items()):
        qs = sorted(qs, key=lambda q: q.strike)
        for lo, hi in zip(qs, qs[1:]):
            width = hi.strike - lo.strike
            # **Each direction is priced on the side you would actually hit, and
            # getting this wrong is the easy mistake.** The first version tested
            # the BUY cost against both bounds and reported 168 violations on a
            # surface that is arbitrage-free by construction. Paying more than
            # the width to own a spread is not an arbitrage for you -- it is a
            # bad trade. The arbitrage is SELLING it for more than its maximum
            # liability, and the proceeds are `2 * half_spread` below the buy
            # cost, which is what made the false positives. A long spread is
            # long the nearer strike for calls and the further one for puts.
            long_leg, short_leg = (lo, hi) if is_call else (hi, lo)
            cost_buy = long_leg.ask - short_leg.bid
            buy_size = min(long_leg.ask_size, short_leg.bid_size)
            proceeds_sell = long_leg.bid - short_leg.ask
            sell_size = min(long_leg.bid_size, short_leg.ask_size)
            if cost_buy < -tol and buy_size >= 1:
                out.append({"expiry_session": expiry, "is_call": is_call,
                            "lo": lo.strike, "hi": hi.strike,
                            "edge": -cost_buy, "size": buy_size,
                            "why": "paid to own a non-negative payoff"})
            if proceeds_sell - width > tol and sell_size >= 1:
                out.append({"expiry_session": expiry, "is_call": is_call,
                            "lo": lo.strike, "hi": hi.strike,
                            "edge": proceeds_sell - width, "size": sell_size,
                            "why": "received more than the maximum liability"})
    return out


def butterfly_violations(quotes: list[OptionQuote], tol: float = 1e-9) -> list[dict]:
    """Convexity in K, for triples that are NOT necessarily equally spaced.

    **The equal-spacing version is wrong on a real strike ladder.** QQQ lists
    $1 strikes near the money and $5 in the wings, so consecutive triples
    straddling the boundary -- (475, 480, 481), (519, 520, 525) -- are not
    butterflies at all, and `a.ask - 2*b.bid + c.ask` reported six violations
    on a surface that is arbitrage-free by construction. Convexity still
    applies there; it just is not a 1-2-1.

    For K1 < K2 < K3, convexity requires

        C(K2) <= w * C(K1) + (1 - w) * C(K3),   w = (K3 - K2) / (K3 - K1)

    so the package is: buy `w` of K1, buy `1 - w` of K3, sell one K2. At equal
    spacing w = 1/2 and this is the familiar 1-2-1 halved. The edge is reported
    per unit of the K2 leg; the fractional weights scale to whole contracts --
    (475, 480, 481) has w = 1/6, which is 1 x K1 and 5 x K3 against 6 x K2.
    """
    out = []
    groups: dict[tuple[int, bool], list[OptionQuote]] = {}
    for q in quotes:
        groups.setdefault((q.expiry_session, q.is_call), []).append(q)
    for (expiry, is_call), qs in sorted(groups.items()):
        qs = sorted(qs, key=lambda q: q.strike)
        for a, b, c in zip(qs, qs[1:], qs[2:]):
            span = c.strike - a.strike
            if span <= 0:
                continue
            w = (c.strike - b.strike) / span
            cost = w * a.ask + (1.0 - w) * c.ask - b.bid
            # the wings are bought and the body sold, so those are the sides
            # whose size binds
            size = min(a.ask_size, c.ask_size, b.bid_size)
            if cost < -tol and size >= 1:
                out.append({"expiry_session": expiry, "is_call": is_call,
                            "strikes": (a.strike, b.strike, c.strike),
                            "weight_low": round(w, 6), "edge": -cost,
                            "size": size})
    return out


def calendar_violations(quotes: list[OptionQuote], tol: float = 1e-9) -> list[dict]:
    """At one strike, a nearer expiry cannot be dearer than a further one."""
    out = []
    groups: dict[tuple[float, bool], list[OptionQuote]] = {}
    for q in quotes:
        groups.setdefault((q.strike, q.is_call), []).append(q)
    for (strike, is_call), qs in sorted(groups.items()):
        qs = sorted(qs, key=lambda q: q.expiry_session)
        for near, far in zip(qs, qs[1:]):
            edge = near.bid - far.ask         # sell the near, buy the far
            size = min(near.bid_size, far.ask_size)
            if edge > tol and size >= 1:
                out.append({"strike": strike, "is_call": is_call,
                            "near": near.expiry_session, "far": far.expiry_session,
                            "edge": edge, "size": size})
    return out


ALL_CHECKS = {
    "parity": parity_violations,
    "vertical": vertical_violations,
    "butterfly": butterfly_violations,
    "calendar": calendar_violations,
}


class OptionsFeed:
    """Serves the surface as of an instant, and cannot look ahead.

    **The forward is the engine's `last_price`, which is why this is safe.** An
    options feed could have been a second stream of quote events with its own
    timestamps, and then the engine would have had to decide which of them a
    strategy may see at instant t -- a look-ahead question with a wrong answer
    available. Here the surface is a deterministic function of the underlying
    price, so the snapshot at t is computed from the last trade at or before t
    and there is no future quote to leak: the only input is a number the
    strategy could already read through `ctx.last_price`.

    That also makes the feed free of the walk. Two runs at the same seed see
    identical surfaces because they see identical prices, with no separate
    stream to keep in step.

    Snapshots are cached on the forward quantised to the underlying tick, since
    a strategy reading the surface every minute of a session asks for the same
    few hundred distinct prices many times over.
    """

    __slots__ = ("surface", "_cache", "_cap", "hits", "misses")

    def __init__(self, surface: SyntheticOptionsSurface, cache_size: int = 512):
        self.surface = surface
        self._cache: dict[tuple[int, int], list[OptionQuote]] = {}
        self._cap = cache_size
        self.hits = self.misses = 0

    def snapshot(self, ts: int, session: int, forward: float) -> list[OptionQuote]:
        tick = self.surface.spec.underlying_tick
        key = (session, int(round(forward / tick)))
        hit = self._cache.get(key)
        if hit is not None:
            self.hits += 1
            # **`replace`, not a hand-listed constructor call.** The quotes are
            # identical and only the instant they are read at moves -- but the
            # first version enumerated the fields, so when `bid_size` and
            # `ask_size` were added every CACHED read silently returned size
            # zero and the size-aware checks refused to report anything. The
            # plant went from being found in 2,808 of 2,808 bars to 149, which
            # is exactly the number of cache MISSES. Copying by field is a
            # standing invitation to that bug; `replace` cannot drop one.
            return [replace(q, ts=ts) for q in hit]
        self.misses += 1
        quoted = round(round(forward / tick) * tick, 6)
        out = self.surface.quotes_at(ts, session, quoted)
        if len(self._cache) >= self._cap:
            self._cache.clear()        # cheaper than an LRU and bounded either way
        self._cache[key] = out
        return out

    def capabilities_note(self) -> str:
        return (f"{len(self.surface.strikes(self.surface.spec.start_price))} strikes x "
                f"{len(self.surface.spec.expiries_in_sessions)} expiries x 2 sides")


class SyntheticOptionsDataset:
    """The underlying from `SyntheticDataset`, the surface from this module.

    **Composed rather than reimplemented.** The tick generator already carries
    the calendar, the splits, the embargo, session irregularity and the
    holdout lock, and every one of those has a defect history behind it -- a
    second copy for options would reacquire them. So this delegates everything
    about the walk and adds exactly one thing: `options()`.
    """

    def __init__(self, underlying, surface: SyntheticOptionsSurface | None = None):
        from src.core.types import DataCapabilities
        self._under = underlying
        self._surface = surface or SyntheticOptionsSurface()
        self._feed = OptionsFeed(self._surface)
        self.name = f"{underlying.name}+{self._surface.name}"
        base = underlying.capabilities()
        self._caps = DataCapabilities(has_trades=base.has_trades,
                                      has_quotes=base.has_quotes,
                                      has_book=base.has_book,
                                      has_options=True)

    # ---- the options channel ---------------------------------------------- #
    def options(self) -> OptionsFeed:
        return self._feed

    # ---- everything else is the underlying's ------------------------------ #
    def capabilities(self):
        return self._caps

    def calendar(self):
        return self._under.calendar()

    def ticks(self, split, *, warmup: int = 0):
        # `split` is required, matching the Dataset protocol. Defaulting it to
        # None here implied None was a valid split; NQDataset tolerates that and
        # SyntheticDataset raises KeyError deep inside `calendar.bounds`, so the
        # wrapper was offering a signature neither underlying honours.
        return self._under.ticks(split, warmup=warmup)

    def session_bounds(self, session: int):
        return self._under.session_bounds(session)

    # `sessions()` is deliberately NOT delegated explicitly: NQDataset has it
    # and SyntheticDataset does not, so naming it here would promise a method
    # that may not exist. `__getattr__` forwards it when the underlying has one
    # and raises the underlying's own AttributeError when it does not, which is
    # the honest answer.

    def digest(self) -> str:
        """Both halves, because a surface change is a different dataset.

        The underlying's digest alone would make a run on a surface with three
        plants indistinguishable in the record from a run on a clean one.
        """
        return hashlib.sha256(
            f"{self._under.digest()}|{self._surface.spec.digest()}".encode()
        ).hexdigest()[:16]

    def __getattr__(self, name):
        """Anything else -- `unlock_holdout`, `scored_sessions`, `early_close_dates`
        -- is the underlying's, so the lock and the calendar keep working rather
        than being quietly absent on an options run."""
        return getattr(self._under, name)
