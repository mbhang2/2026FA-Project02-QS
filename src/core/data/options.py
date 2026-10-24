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
from dataclasses import dataclass, field

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

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid + self.ask)


@dataclass(frozen=True)
class SyntheticOptionsSpec:
    # ---- the surface ------------------------------------------------------ #
    n_strikes: int = 41                   # odd, so one strike sits at the money
    strike_spacing: float = 1.0
    expiries_in_sessions: tuple[int, ...] = (1, 2, 5, 10, 21)
    # ---- resolution ------------------------------------------------------- #
    quotes_per_session: int = 390         # one a minute over 6.5h
    quote_spacing_ns: int = 60 * NS_PER_SECOND
    # ---- the underlying --------------------------------------------------- #
    start_price: float = 100.0
    vol_per_session: float = 1.0          # sd of the underlying over one session
    tick_size: float = 0.25
    # ---- the quote --------------------------------------------------------- #
    half_spread: float = 0.05             # per side, in price units
    # ---- the plant --------------------------------------------------------- #
    arb_strength: float = 0.0             # 0 = nothing to find
    arb_expiry_index: int = 2             # which expiry carries the violation
    arb_strike_offset: int = 0            # strikes away from the money
    # ---- bookkeeping ------------------------------------------------------- #
    seed: int = 0
    n_sessions: int = 120
    session_gap_ns: int = 17 * 3600 * NS_PER_SECOND
    density_grid_halfwidth: int = 240     # grid points either side, in ticks

    def digest(self) -> str:
        parts = [f"{k}={getattr(self, k)!r}" for k in sorted(self.__dataclass_fields__)]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


class SyntheticOptionsSurface:
    """Prices every option by discounted expectation under one discrete density.

    Rates are zero, deliberately. A non-zero rate adds a discount factor to
    every leg and changes nothing about which conditions hold, while giving
    parity a term whose sign a reader has to track. The point of this dataset is
    that parity is exact; r=0 keeps it legible.
    """

    def __init__(self, spec: SyntheticOptionsSpec | None = None) -> None:
        self.spec = spec or SyntheticOptionsSpec()
        s = self.spec
        if s.n_strikes % 2 == 0:
            raise ValueError("n_strikes must be odd so one strike is at the money")
        if not s.expiries_in_sessions or min(s.expiries_in_sessions) < 1:
            raise ValueError("expiries are counted in whole sessions, at least 1")
        if sorted(s.expiries_in_sessions) != list(s.expiries_in_sessions):
            raise ValueError("expiries must be given in increasing order")
        if s.arb_expiry_index not in range(len(s.expiries_in_sessions)):
            raise ValueError("arb_expiry_index is not one of the expiries")
        self.name = (f"synthetic-options:arb={s.arb_strength:g}:seed={s.seed}")
        self._densities: dict[int, np.ndarray] = {}
        self._offsets = (
            np.arange(-s.density_grid_halfwidth, s.density_grid_halfwidth + 1)
            * s.tick_size
        )

    # ---- the density ------------------------------------------------------ #
    def _kernel(self) -> np.ndarray:
        """One session of diffusion, on the tick grid, as a normalised kernel."""
        s = self.spec
        sd_ticks = s.vol_per_session / s.tick_size
        half = max(1, int(np.ceil(4.0 * sd_ticks)))
        x = np.arange(-half, half + 1, dtype=float)
        k = np.exp(-0.5 * (x / sd_ticks) ** 2)
        return k / k.sum()

    def density(self, sessions_ahead: int) -> np.ndarray:
        """The terminal density `sessions_ahead` sessions out, on `self._offsets`.

        Built by convolving ONE kernel repeatedly, which is what makes the
        expiries mutually consistent: the 10-session density is the 5-session
        density diffused further, so variance increases monotonically and no
        calendar arbitrage can appear between them.
        """
        if sessions_ahead in self._densities:
            return self._densities[sessions_ahead]
        d = np.zeros_like(self._offsets)
        d[len(d) // 2] = 1.0                       # a point mass at the forward
        k = self._kernel()
        for _ in range(sessions_ahead):
            d = np.convolve(d, k, mode="same")
        total = d.sum()
        if total <= 0:
            raise ValueError("density collapsed; widen density_grid_halfwidth")
        d = d / total                              # renormalise the clipped tails
        self._densities[sessions_ahead] = d
        return d

    # ---- pricing ---------------------------------------------------------- #
    def price(self, forward: float, strike: float, sessions_ahead: int,
              is_call: bool) -> float:
        """Discounted expectation of the payoff. r = 0, so no discount factor."""
        d = self.density(sessions_ahead)
        terminal = forward + self._offsets
        payoff = (np.maximum(terminal - strike, 0.0) if is_call
                  else np.maximum(strike - terminal, 0.0))
        return float(np.dot(d, payoff))

    def strikes(self, forward: float) -> list[float]:
        """Strikes centred on the money, on a fixed grid so the ladder does not
        move under the strategy's feet mid-session."""
        s = self.spec
        atm = round(forward / s.strike_spacing) * s.strike_spacing
        half = s.n_strikes // 2
        return [atm + (i - half) * s.strike_spacing for i in range(s.n_strikes)]

    # ---- the plant -------------------------------------------------------- #
    def _planted_pair(self, forward: float) -> tuple[int, float]:
        s = self.spec
        expiry = s.expiries_in_sessions[s.arb_expiry_index]
        ks = self.strikes(forward)
        strike = ks[len(ks) // 2 + s.arb_strike_offset]
        return expiry, strike

    def oracle(self) -> dict:
        """What the planted arbitrage is worth per unit, on the OPTION legs.

        **Two legs, not four.** The parity package is: buy the call at its ask,
        sell the put at its bid. That is `2 * half_spread` of crossing, and the
        plant makes the package cheaper than `forward - strike` by
        `arb_strength`, so the edge is `arb_strength - 2 * half_spread`. An
        earlier version charged four half-spreads on the theory that a
        conversion has four sides; it has three -- call, put and underlying --
        and it disagreed with what `parity_violations` actually reports by
        exactly `2 * half_spread`. The checker was right.

        **The underlying leg is deliberately not priced here.** Closing the
        conversion needs the futures leg, and what that costs is the engine's
        fill model and commission, not a property of this surface. Quoting it
        here would be this file asserting something it does not own. So
        `net_per_unit` is an upper bound on what is capturable: a loop cannot
        beat it, and the gap between it and a realised result is execution.

        Nothing above is fitted, which is the point of a planted arbitrage -- it
        is a number, not an estimate.
        """
        s = self.spec
        gross = s.arb_strength
        option_legs = 2.0 * s.half_spread
        net = gross - option_legs
        return {
            "planted": gross,
            "option_leg_cost_per_unit": option_legs,
            "net_per_unit": net,
            "findable": net > 0.0,
            "underlying_leg": ("not priced here; the futures side is the engine's "
                               "fill model and commission"),
            "visible_routes": self._visible_routes(),
            "note": ("net_per_unit is an UPPER BOUND on the capturable edge, on "
                     "the option legs alone. findable is false when the plant is "
                     "smaller than the spread it must clear, which is a "
                     "deliberately available configuration: a violation that "
                     "exists and cannot be monetised is the case that says "
                     "whether the loop reports edges it cannot capture."),
        }

    def _visible_routes(self) -> list[str]:
        """Which no-arbitrage conditions the plant breaks at this strength.

        Measured rather than reasoned: the thresholds are read off the default
        surface and are in the module docstring. Reported so a campaign's record
        says how many ways there were to find the thing it was asked to find.
        """
        g = self.spec.arb_strength
        h = self.spec.half_spread
        routes = []
        if g > 2.0 * h:
            routes.append("parity")
        if g >= 0.40:
            routes.append("butterfly")
        if g >= 0.50:
            routes.append("calendar")
        if g >= 0.75:
            routes.append("vertical")
        return routes

    # ---- quotes ----------------------------------------------------------- #
    def quotes_at(self, ts: int, session: int, forward: float) -> list[OptionQuote]:
        """The whole surface at one instant: every strike, every expiry, both
        sides. The cross-section is the point -- an arbitrage is a statement
        about several quotes at once, so a feed that delivered them one at a
        time would make the task about reassembly rather than detection."""
        s = self.spec
        out: list[OptionQuote] = []
        arb_expiry, arb_strike = self._planted_pair(forward)
        for expiry in s.expiries_in_sessions:
            for strike in self.strikes(forward):
                for is_call in (True, False):
                    mid = self.price(forward, strike, expiry, is_call)
                    if (s.arb_strength and is_call
                            and expiry == arb_expiry and strike == arb_strike):
                        mid -= s.arb_strength      # the only perturbation
                    bid = max(0.0, mid - s.half_spread)
                    ask = mid + s.half_spread
                    out.append(OptionQuote(
                        ts=ts, session=session, expiry_session=session + expiry,
                        strike=float(strike), is_call=is_call,
                        bid=round(bid, 6), ask=round(ask, 6)))
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
        synthetic_long = c.ask - p.bid          # cost to be long via options
        fair = forward - strike
        # long the package cheaper than fair, or short it dearer than fair
        cheap = fair - synthetic_long
        synthetic_short = c.bid - p.ask
        dear = synthetic_short - fair
        edge = max(cheap, dear)
        if edge > tol:
            out.append({"expiry_session": expiry, "strike": strike,
                        "edge": edge,
                        "side": "long_via_options" if cheap >= dear else "short_via_options"})
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
            proceeds_sell = long_leg.bid - short_leg.ask
            if cost_buy < -tol:
                out.append({"expiry_session": expiry, "is_call": is_call,
                            "lo": lo.strike, "hi": hi.strike,
                            "edge": -cost_buy,
                            "why": "paid to own a non-negative payoff"})
            if proceeds_sell - width > tol:
                out.append({"expiry_session": expiry, "is_call": is_call,
                            "lo": lo.strike, "hi": hi.strike,
                            "edge": proceeds_sell - width,
                            "why": "received more than the maximum liability"})
    return out


def butterfly_violations(quotes: list[OptionQuote], tol: float = 1e-9) -> list[dict]:
    """A butterfly cannot be bought for less than nothing: convexity in K."""
    out = []
    groups: dict[tuple[int, bool], list[OptionQuote]] = {}
    for q in quotes:
        groups.setdefault((q.expiry_session, q.is_call), []).append(q)
    for (expiry, is_call), qs in sorted(groups.items()):
        qs = sorted(qs, key=lambda q: q.strike)
        for a, b, c in zip(qs, qs[1:], qs[2:]):
            cost = a.ask - 2.0 * b.bid + c.ask
            if cost < -tol:
                out.append({"expiry_session": expiry, "is_call": is_call,
                            "strikes": (a.strike, b.strike, c.strike),
                            "edge": -cost})
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
            if edge > tol:
                out.append({"strike": strike, "is_call": is_call,
                            "near": near.expiry_session, "far": far.expiry_session,
                            "edge": edge})
    return out


ALL_CHECKS = {
    "parity": parity_violations,
    "vertical": vertical_violations,
    "butterfly": butterfly_violations,
    "calendar": calendar_violations,
}
