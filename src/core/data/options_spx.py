"""An SPX options surface from a calibrated Heston model, arbitrage-free by
construction, with optional planted arbitrage.

    SPXHestonSurface()                       # nothing to find
    SPXHestonSurface(plants=(Plant(...),))   # a violation of known size

**Why this exists beside the Gaussian surface in `options.py`.** That one is a
single normal density: flat implied vol, no skew, 16% everywhere. Useful for
proving the machinery, and nothing like SPX. This one takes a real calibration
and produces a real surface -- measured below at +9.15 volatility points of
90/110 skew at the front maturity, flattening to +2.31 at four years, over a
rising ATM term structure of 13.6% / 16.4% / 19.1%.

**The design survives the swap unchanged, and that is the whole point of having
priced by density in the first place.** Every no-arbitrage condition is a
property of `max(S - K, 0)` under SOME valid probability measure, not of the
normal distribution:

    parity      C - P = D * sum_j p_j (S_j - K) = D * (F - K), exactly
    vertical    monotone in K
    butterfly   convex in K

So replacing a Gaussian density with a Heston one keeps all of them. What it
buys is skew, term structure and the right tail shape. **The rule that must not
be broken is to go through the DENSITY and never through the prices**: pricing
directly by Fourier inversion produces small convexity violations from
numerical truncation, which would put unplanted arbitrage back into the
baseline and make the exercise unfalsifiable again.

**SPX rather than QQQ, and not only for the calibration.** SPX options are
European and cash-settled. For American options -- QQQ's -- put-call parity is
an INEQUALITY carrying early-exercise and dividend terms, which is why most
apparent parity violations in real American data are not arbitrage at all. On
SPX, parity genuinely is an identity, so the exact-parity construction here
stops being a simplification and becomes correct.

## The calibration

Michael's, 2026-10-12 13:56:47 CT, SPX = 7,799.26. Heston fitted to 197 surface
points at an RMSE of 0.35 volatility points:

    kappa 2.092   theta 0.0542   eta 0.981   rho -0.667   v0 0.0211

**The Feller condition is violated**: 2*kappa*theta = 0.2268 against
eta^2 = 0.9624. That is common in calibrated equity parameters and it is why
`_cf` uses the Albrecher "little trap" form of the characteristic function. The
naive branch, g = (xi + d)/(xi - d), has |g| > 1 and its logarithm wraps at long
maturities -- silently, with plausible-looking prices. `phi(-i) = 1` to twelve
decimals is the check that the branch is right, because that identity IS the
martingale condition.

Verified on arrival: the three forwards reconstruct from S, r and q to within
1.22 points on 9,417 (0.013%), and each maturity's T recovered from its own
discount factor agrees with the calendar.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

import numpy as np

from .synthetic import SESSION_ORIGIN_NS, SyntheticDataset, SyntheticSpec
from ..types import DataCapabilities, Side, Tick
from .options import (                        # noqa: F401  (re-exported)
    OptionQuote, Plant, butterfly_violations, vertical_violations,
    parse_option_asset, is_option_asset,
    option_asset as _option_asset, option_asset_of as _option_asset_of,
)


@dataclass(frozen=True, slots=True)
class Maturity:
    """One listed expiry, as the calibration gives it."""

    label: str
    forward: float
    discount: float          # D = exp(-r T)
    r: float
    q: float

    @property
    def T(self) -> float:
        """Recovered from the discount factor rather than from a day count.

        The two agree here to five decimals, and taking it from D keeps the
        discounting self-consistent: a T from ACT/365 and a D from the quote
        sheet would disagree in the last place and put that disagreement into
        every price.
        """
        return -math.log(self.discount)/self.r


# Michael's calibration, 2026-10-12 13:56:47 CT.
SPOT = 7799.26
KAPPA, THETA, ETA, RHO, V0 = 2.092, 0.0542, 0.981, -0.667, 0.0211
MATURITIES = (
    Maturity("2026-12-18", 7847.75, 0.99200, 0.0439, 0.0100),
    Maturity("2027-12-17", 8187.41, 0.94204, 0.0506, 0.0094),
    Maturity("2030-12-20", 9417.14, 0.79944, 0.0534, 0.0084),
)


@dataclass(frozen=True)
class SPXSpec:
    """Market conventions, the quote model, and what was planted."""

    # ---- the ladder ------------------------------------------------------- #
    # SPX lists 5-point strikes around the money and 25-point strikes further
    # out. A uniform grid would hide the thing the QQQ surface taught: three
    # strikes are only a butterfly when equally spaced, and at the boundary
    # they are not.
    near_spacing: float = 5.0
    wide_spacing: float = 25.0
    near_halfwidth_frac: float = 0.06      # +/- 6% of the forward at 5 points
    wide_halfwidth_frac: float = 0.25      # out to 25% at 25 points

    # ---- the quote -------------------------------------------------------- #
    # SPX premium quotes step in $0.05, and $0.10 for the higher premiums.
    min_half_spread: float = 0.10
    half_spread_frac: float = 0.004        # of premium
    far_expiry_widening: float = 0.25      # per year of maturity, in points
    dime_above: float = 3.00
    nickel_increment: float = 0.05
    dime_increment: float = 0.10

    # ---- numerics --------------------------------------------------------- #
    cos_terms: int = 4096
    cos_width: float = 12.0                # sds of integrated variance
    grid_points: int = 2001

    # ---- quoted size ------------------------------------------------------- #
    # Smaller than the QQQ model in contracts, because an SPX contract is worth
    # a hundred times the index. Shape rather than a fit: what matters is that
    # size varies, so a detector has to ask whether an edge is takeable.
    size_at_the_money: int = 30
    size_decay_moneyness: float = 0.05
    size_maturity_decay: float = 0.30      # per year of maturity

    plants: tuple[Plant, ...] = ()

    def digest(self) -> str:
        parts = [f"{k}={getattr(self, k)!r}" for k in sorted(self.__dataclass_fields__)]
        parts.append(f"cal={KAPPA},{THETA},{ETA},{RHO},{V0},{SPOT}")
        parts += [f"{m.label}:{m.forward}:{m.discount}" for m in MATURITIES]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


class SPXHestonSurface:
    """Prices every option by discounted expectation under a Heston density."""

    def __init__(self, spec: SPXSpec | None = None,
                 maturities: tuple[Maturity, ...] = MATURITIES):
        self.spec = spec or SPXSpec()
        self.maturities = maturities
        for pl in self.spec.plants:
            if pl.kind not in Plant.KINDS:
                raise ValueError(f"unknown plant kind {pl.kind!r}")
            if pl.expiry_index not in range(len(maturities)):
                raise ValueError(f"plant expiry_index {pl.expiry_index} is not a maturity")
            if pl.kind == "calendar":
                raise ValueError(
                    "calendar plants are not supported on this surface, and the "
                    "reason is the carry rather than an omission. Same-strike "
                    "calendar monotonicity holds when the expiries SHARE a "
                    "forward -- options on one futures contract, or r = q = 0 as "
                    "on the Gaussian surface. Here r > q, so the forward RISES "
                    "with maturity (7847.75 -> 8187.41 -> 9417.14) and the same "
                    "strike is legitimately deeper in the money at the near "
                    "date: the Dec-26 put at 8575 is worth 728.42 against "
                    "Dec-27's 720.58, which is not arbitrage. Selling the near "
                    "and buying the far replicates nothing, because after the "
                    "near expires you have paid out and the far option's value "
                    "then is a random variable. A correct check would compare "
                    "NORMALISED prices at matched MONEYNESS, which needs "
                    "interpolation between listed strikes and so carries its own "
                    "error budget. Use parity or butterfly, both of which live "
                    "inside a single maturity and are untouched by carry.")
        self.name = (f"spx-heston:plants={len(self.spec.plants)}:"
                     f"exp={'/'.join(str(e) for e in expiry_sessions(maturities))}:"
                     f"{self.digest()[:8]}")
        self._dens: dict[int, tuple] = {}

    def digest(self) -> str:
        """The spec AND the maturities this surface actually lists.

        `SPXSpec.digest` hashes the module-level calibration, which is right
        for the conventions and wrong for identity: it is the same object no
        matter which expiries a surface was built with, so a short-dated
        surface and the calibrated one hashed IDENTICALLY. Two datasets that
        cannot be told apart in the run record is the one failure a digest
        exists to prevent, and here it would have been two datasets whose
        options expire 46 and 1,056 sessions apart.
        """
        mats = "|".join(f"{m.label}:{m.forward:.4f}:{m.discount:.6f}"
                        for m in self.maturities)
        return hashlib.sha256(
            f"{self.spec.digest()}|{mats}".encode()).hexdigest()[:16]

    # ---- the model -------------------------------------------------------- #
    @staticmethod
    def _cf(u, T: float):
        """E[exp(i u ln(S_T/F))], Albrecher "little trap" form.

        Feller is violated by this calibration, so the branch matters: the
        naive g has modulus above one and `log(1 - g e^{-dT})` wraps at long
        maturities, producing wrong prices that look reasonable.
        """
        u = np.asarray(u, dtype=complex)
        xi = KAPPA - 1j*RHO*ETA*u
        d = np.sqrt(xi**2 + (ETA**2)*(u**2 + 1j*u))
        g = (xi - d)/(xi + d)
        e = np.exp(-d*T)
        C = (KAPPA*THETA/ETA**2)*((xi - d)*T - 2.0*np.log((1.0 - g*e)/(1.0 - g)))
        D = ((xi - d)/ETA**2)*((1.0 - e)/(1.0 - g*e))
        return np.exp(C + D*V0)

    @staticmethod
    def expected_integrated_variance(T: float) -> float:
        return THETA*T + (V0 - THETA)*(1.0 - math.exp(-KAPPA*T))/KAPPA

    def density(self, i: int) -> tuple:
        """`(terminal_prices, probabilities)` for maturity `i`, by COS.

        Negative lobes from truncation are clipped and the mass renormalised,
        so what pricing sees is always a valid probability measure -- which is
        what every no-arbitrage guarantee rests on. On this calibration there
        is nothing to clip: the minimum density value is positive at all three
        maturities, which is asserted rather than assumed.
        """
        if i in self._dens:
            return self._dens[i]
        s, m = self.spec, self.maturities[i]
        T = m.T
        sd = math.sqrt(max(self.expected_integrated_variance(T), 1e-12))
        a, b = -s.cos_width*sd, s.cos_width*sd
        k = np.arange(s.cos_terms)
        uk = k*np.pi/(b - a)
        coef = np.real(self._cf(uk, T)*np.exp(-1j*uk*a))
        coef[0] *= 0.5
        y = np.linspace(a, b, s.grid_points)
        f = (2.0/(b - a))*(np.cos(uk[None, :]*(y[:, None] - a)) @ coef)
        f = np.clip(f, 0.0, None)
        dy = y[1] - y[0]
        total = f.sum()*dy
        if total <= 0:
            raise ValueError("the COS density collapsed; widen cos_width")
        prob = f*dy/total
        out = (m.forward*np.exp(y), prob)
        self._dens[i] = out
        return out

    # ---- pricing ---------------------------------------------------------- #
    def call_prices(self, i: int, strikes) -> np.ndarray:
        terminal, prob = self.density(i)
        k = np.asarray(strikes, dtype=float)
        payoff = np.maximum(terminal[None, :] - k[:, None], 0.0)
        return self.maturities[i].discount*(payoff @ prob)

    def put_prices(self, i: int, strikes) -> np.ndarray:
        """From the calls by parity: P = C - D (F - K).

        Derived rather than priced again, so parity is an identity of the
        implementation and cannot drift. With D < 1 the discount factor is
        explicit here; the Gaussian surface took r = 0 and could leave it out.
        """
        m = self.maturities[i]
        k = np.asarray(strikes, dtype=float)
        return self.call_prices(i, k) - m.discount*(m.forward - k)

    def implied_vol(self, i: int, strike: float, call: float | None = None) -> float:
        """Black-76 implied vol, for validating the surface against the fit."""
        m = self.maturities[i]
        price = float(self.call_prices(i, [strike])[0]) if call is None else call
        lo, hi = 1e-6, 4.0
        if price <= m.discount*max(m.forward - strike, 0.0) + 1e-10:
            return float("nan")
        N = lambda x: 0.5*(1.0 + math.erf(x/math.sqrt(2.0)))        # noqa: E731
        for _ in range(100):
            mid = 0.5*(lo + hi)
            v = mid*math.sqrt(m.T)
            d1 = (math.log(m.forward/strike) + 0.5*v*v)/v
            bs = m.discount*(m.forward*N(d1) - strike*N(d1 - v))
            if bs < price:
                lo = mid
            else:
                hi = mid
        return hi

    # ---- the ladder ------------------------------------------------------- #
    def strikes(self, i: int) -> list[float]:
        s, m = self.spec, self.maturities[i]
        atm = round(m.forward/s.near_spacing)*s.near_spacing
        near = s.near_halfwidth_frac*m.forward
        wide = s.wide_halfwidth_frac*m.forward
        out = {atm + j*s.near_spacing
               for j in range(-int(near/s.near_spacing), int(near/s.near_spacing) + 1)}
        step = s.wide_spacing
        kk = round((atm - wide)/step)*step
        while kk <= atm + wide:
            if abs(kk - atm) > near:
                out.add(kk)
            kk += step
        return sorted(round(x, 6) for x in out if x > 0)

    # ---- the quote -------------------------------------------------------- #

    def sizes(self, mid: float, strike: float, forward: float,
              tau: float) -> tuple[int, int]:
        """Quoted size, thickest at the money and thinning with the wings and
        with maturity. No bid where the premium rounds to nothing: a 0.00 bid is
        nobody bidding, not a thin bid."""
        s = self.spec
        if forward <= 0 or strike <= 0:
            return 0, 0
        decay = math.exp(-(math.log(strike/forward)/s.size_decay_moneyness)**2)
        n = max(1, int(round(s.size_at_the_money*decay/(1.0 + s.size_maturity_decay*tau))))
        return n, n

    def increment(self, mid: float) -> float:
        s = self.spec
        return s.dime_increment if mid >= s.dime_above else s.nickel_increment

    def half_spread(self, mid: float, i: int) -> float:
        s = self.spec
        return (s.min_half_spread + s.half_spread_frac*mid
                + s.far_expiry_widening*self.maturities[i].T)

    def _quote(self, mid: float, i: int) -> tuple[float, float]:
        """Rounded AWAY from the mid, always.

        Rounding to nearest would move a bid up or an ask down by half an
        increment and manufacture edge nothing planted. Away-from-mid can only
        cost edge, which is why `oracle()` reports a bound.
        """
        h = self.half_spread(mid, i)
        inc = self.increment(mid)
        bid = max(0.0, math.floor((mid - h)/inc)*inc)
        ask = math.ceil((mid + h)/inc)*inc
        return round(bid, 4), round(ask, 4)

    # ---- the plants ------------------------------------------------------- #
    def _sites(self) -> dict:
        sites: dict = {}
        for pl in self.spec.plants:
            ks = self.strikes(pl.expiry_index)
            m = self.maturities[pl.expiry_index]
            atm = min(range(len(ks)), key=lambda j: abs(ks[j] - m.forward))
            j = min(max(atm + pl.strike_offset, 0), len(ks) - 1)
            sign = -1.0 if pl.kind == "parity" else +1.0
            sites[(pl.expiry_index, ks[j], True)] = sign*pl.strength
        return sites

    def quotes_at(self, ts: int, session: int) -> list[OptionQuote]:
        """The whole surface: every strike, every maturity, both sides.

        `expiry_session` carries the expiry as a count of sessions from the
        valuation date -- see `sessions_to_expiry`, which explains why this is
        not the maturity index it used to be. Use `maturity_index` to go back.
        """
        sites = self._sites()
        exp = expiry_sessions(self.maturities)
        out: list[OptionQuote] = []
        for i, m in enumerate(self.maturities):
            ks = self.strikes(i)
            calls, puts = self.call_prices(i, ks), self.put_prices(i, ks)
            for j, strike in enumerate(ks):
                for is_call, mid in ((True, float(calls[j])), (False, float(puts[j]))):
                    mid = max(mid + sites.get((i, strike, is_call), 0.0), 0.0)
                    bid, ask = self._quote(mid, i)
                    bn, an = self.sizes(mid, strike, m.forward, m.T)
                    if bid <= 0.0:
                        bn = 0   # a 0.00 bid is nobody bidding
                    out.append(OptionQuote(ts=ts, session=session,
                                           expiry_session=exp[i],
                                           strike=float(strike), is_call=is_call,
                                           bid=bid, ask=ask, bid_size=bn, ask_size=an))
        return out

    def maturity_index(self, expiry_session: int) -> int:
        """The index into `maturities` of the expiry `expiry_session` names.

        The inverse of what `quotes_at` writes. Callers that need the maturity
        itself -- to re-price a quote, or to read its forward -- need this,
        because the quote no longer carries a position in the list.
        """
        for i, e in enumerate(expiry_sessions(self.maturities)):
            if e == expiry_session:
                return i
        raise KeyError(
            f"no listed maturity expires at session {expiry_session}; "
            f"this surface lists {expiry_sessions(self.maturities)}")

    # ---- the oracle ------------------------------------------------------- #
    def threshold(self, plant: Plant) -> dict:
        """What the plant's named condition must clear before it is findable.

        Only parity has a zero intrinsic term, because only parity is an
        identity. A calendar plant must first clear the premium between its two
        maturities, which on this surface is large: these expiries are years
        apart, not days.
        """
        i = plant.expiry_index
        ks = self.strikes(i)
        m = self.maturities[i]
        atm = min(range(len(ks)), key=lambda j: abs(ks[j] - m.forward))
        j = min(max(atm + plant.strike_offset, 0), len(ks) - 1)
        strike = ks[j]
        c = float(self.call_prices(i, [strike])[0])
        # the mid this plant will actually be quoted from
        c_q = max(c - plant.strength, 0.0) if plant.kind == "parity" else c + plant.strength
        if plant.kind == "parity":
            p = float(self.put_prices(i, [strike])[0])
        # **The spread is computed on the PLANTED mid, not the clean one.**
        # half_spread is proportional to premium, so cheapening a call by the
        # plant also narrows its quoted spread -- the edge available is
        # therefore slightly LARGER than a clean-mid calculation predicts, and
        # the "upper bound" was being exceeded: 18.268 found against 18.265
        # claimed at a strength of 20. Caught by the SPX surface, where the
        # effect clears quantisation; latent and masked on the Gaussian surface,
        # whose nickel grid is coarser than the 0.012 the effect is worth there.
            intrinsic, spread, legs = 0.0, (self.half_spread(c_q, i)
                                            + self.half_spread(p, i)), 2
        elif plant.kind == "butterfly":
            lo, hi = ks[max(j-1, 0)], ks[min(j+1, len(ks)-1)]
            span = hi - lo
            w = (hi - strike)/span if span > 0 else 0.5
            clo = float(self.call_prices(i, [lo])[0])
            chi = float(self.call_prices(i, [hi])[0])
            intrinsic = w*clo + (1-w)*chi - c
            spread = (w*self.half_spread(clo, i) + (1-w)*self.half_spread(chi, i)
                      + self.half_spread(c_q, i))
            legs = 3
        else:
            far = i + 1
            cf_ = float(self.call_prices(far, [strike])[0])
            intrinsic = cf_ - c
            spread = self.half_spread(c_q, i) + self.half_spread(cf_, far)
            legs = 2
        return {"kind": plant.kind, "maturity": m.label, "strike": strike,
                "intrinsic_to_clear": intrinsic, "spread_to_clear": spread,
                "threshold": intrinsic + spread, "legs": legs}

    def oracle(self) -> dict:
        rows = []
        for pl in self.spec.plants:
            t = self.threshold(pl)
            net = pl.strength - t["threshold"]
            rows.append({**t, "planted": pl.strength,
                         "net_per_unit_upper": net, "findable": net > 0.0})
        return {"plants": rows, **self.violations(),
                "note": ("net_per_unit_upper is an upper bound on the option legs "
                         "only: quantisation takes up to one increment per leg, and "
                         "a conversion's index leg is not priced here. The calendar "
                         "route is absent by design -- see `violations`.")}

    def violations(self) -> dict:
        """Every condition that is VALID on this surface, measured.

        **The same-strike calendar check is deliberately not run.** It is
        correct on the Gaussian surface, where r = q = 0 and every expiry shares
        a forward, and it is wrong here: with r > q the forward rises with
        maturity, so the same strike sits deeper in the money at the near date
        and a near option is legitimately dearer. Running it anyway reported 93
        violations on a surface that contains none -- the eighth bug the
        arbitrage-free baseline has caught, and the first where the DEFECT WAS
        IN A CHECK THAT WAS RIGHT SOMEWHERE ELSE.

        Parity and butterfly both live inside a single maturity, so carry does
        not touch them; vertical likewise. Parity needs its own discounted form,
        which is `_parity_for`.
        """
        quotes = self.quotes_at(0, 0)
        found = {"parity": [], "vertical": vertical_violations(quotes),
                 "butterfly": butterfly_violations(quotes)}
        for i, m in enumerate(self.maturities):
            found["parity"] += self._parity_for(
                [q for q in quotes if q.expiry_session == i], m)
        return {
            "routes_seen": {k: len(v) for k, v in found.items()},
            "best_edge_by_route": {k: max((r["edge"] for r in v), default=0.0)
                                   for k, v in found.items()},
            "calendar": ("not checked: invalid with carry, see the docstring"),
        }

    @staticmethod
    def _parity_for(quotes: list[OptionQuote], m: Maturity) -> list[dict]:
        """Parity with discounting: C - P = D (F - K), so the fair value of the
        synthetic long is `D * (F - K)` and not `F - K`.

        `options.parity_violations` assumes r = 0, which is right for that
        surface and wrong here -- using it would report a violation at every
        strike, of size proportional to (1 - D) times the moneyness.
        """
        out = []
        by: dict[float, dict[bool, OptionQuote]] = {}
        for q in quotes:
            by.setdefault(q.strike, {})[q.is_call] = q
        for strike, pair in sorted(by.items()):
            c, p = pair.get(True), pair.get(False)
            if c is None or p is None:
                continue
            fair = m.discount*(m.forward - strike)
            cheap = fair - (c.ask - p.bid)
            dear = (c.bid - p.ask) - fair
            edge = max(cheap, dear)
            if edge > 1e-9:
                out.append({"maturity": m.label, "strike": strike, "edge": edge,
                            "side": "long_via_options" if cheap >= dear else
                                    "short_via_options"})
        return out


# --------------------------------------------------------------------------- #
# A moving surface: the Heston path, and options priced from the state at each
# instant.
# --------------------------------------------------------------------------- #
# A static snapshot has one arbitrage to find, once, which does not test "fast
# and effective" detection at all. What does is a surface that moves: remaining
# maturity shrinks, variance wanders, and the plant's edge breathes with the
# spread around it.
#
# **Priced from (S_t, v_t, tau), not from S_t alone.** The Gaussian surface
# could be a function of the forward because its variance was a constant. Under
# Heston the density depends on the CURRENT variance as well, so the feed is
# driven by the path state at an instant rather than by a price -- and v_t is
# not something a strategy can read, which is correct: implied vol is
# observable, the instantaneous variance of the process is not.
# --------------------------------------------------------------------------- #
QUOTES_PER_SESSION = 390
SESSION_YEARS = 1.0/252.0

# One minute a bar, 390 of them: 09:30 to 16:00, a regular session exactly.
SESSION_TICK_SPACING_NS = 60 * 1_000_000_000
# 24h - 6.5h. Makes the session cycle exactly one day, so the open is the same
# clock time every session instead of drifting backwards through it.
SESSION_GAP_NS = (24*3600 - QUOTES_PER_SESSION*60) * 1_000_000_000
# 2023-11-14 14:30 UTC = 09:30 America/New_York. A FIXED offset with no DST:
# the synthetic calendar has no notion of it, and a clock that jumped twice a
# year would put the same session in two different hour buckets -- which is the
# defect this whole change is about, reintroduced by the back door.
RTH_ORIGIN_NS = 1_699_972_200 * 1_000_000_000

# **How many priced snapshots the feed keeps. It was 4,096, and that is what
# killed the machine.**
#
# Measured 2026-10-25 on this surface: one snapshot is 2,452 quotes at about
# 344 bytes each, so 0.8 MB -- and 4,096 of them is **3.45 GB in one process**,
# or 13.8 GB at the W=4 this project runs, on a 17 GB machine that already
# swaps. A background check run was killed by the OS for low memory, which is
# how the number was found.
#
# The cap was inherited from the Gaussian surface, where a snapshot is a few
# dozen quotes and 4,096 of them is nothing. Here it was not only ruinous, it
# was POINTLESS: the same measurement found **0 hits in 4,095 reads**. A
# callback fires once per bar, each bar resolves to a different path instant,
# and so every read is a miss by construction -- the cache only ever filled up
# and cleared. The reuse that genuinely exists is within a single instant (a
# strategy reading the surface twice in one callback), which needs a handful of
# entries and not four thousand.
SNAPSHOT_CACHE = 16


def sessions_to_expiry(m: Maturity) -> int:
    """`m`'s expiry as a count of sessions from the valuation date.

    **This is what `OptionQuote.expiry_session` has to carry, and for a while
    this surface carried the maturity's INDEX instead.** The field is the only
    thing that identifies a contract through time, and an index is not stable:
    the front month is index 0 today and index 0 again after it expires, so a
    position in `SPX-0-C-7850` would have rolled into a different instrument
    without a single order being placed. It also made settlement impossible to
    express -- the engine settles an option when the closing session reaches
    its expiry, and comparing a session against an index of 0, 1 or 2 would
    have settled all three expiries in the first three sessions of every run.

    The Gaussian surface in `options.py` always emitted `session + expiry`, an
    absolute session, which is the convention the type's own docstring states.
    One field with two meanings across two surfaces is the class of defect this
    repo keeps finding, so the surfaces now agree.

    `max(1, ...)` because an expiry rounding to zero sessions is one that has
    already passed, and nothing can be traded on it.
    """
    return max(1, round(m.T/SESSION_YEARS))


def expiry_sessions(maturities: tuple[Maturity, ...]) -> tuple[int, ...]:
    return tuple(sessions_to_expiry(m) for m in maturities)


class SPXHestonPath:
    """One simulated (S, v) path under the calibrated parameters.

    Full-truncation Euler on the variance, exact-in-expectation on log-spot.
    Full truncation rather than reflection because Feller is violated here --
    the variance process reaches zero, and reflecting at zero biases variance
    upward while truncation is the scheme with the better-documented bias on
    exactly this kind of calibration.

    **Simulated once for the whole dataset and cached, not seeded per session.**
    `SyntheticDataset` seeds per session so a split served alone matches a split
    served with the others, which works because its sessions are independent.
    A Heston path is not: v_t carries across the boundary, and per-session
    seeding would reset it. One seed, one path, sliced by session, is both
    deterministic and continuous.
    """

    def __init__(self, n_sessions: int, seed: int = 0,
                 quotes_per_session: int = QUOTES_PER_SESSION,
                 drift_r: float = 0.0439, drift_q: float = 0.0100):
        self.n_sessions = n_sessions
        self.quotes_per_session = quotes_per_session
        self.seed = seed
        self.dt = SESSION_YEARS/quotes_per_session
        n = n_sessions*quotes_per_session
        # 0x5058 is "PX": a fixed stream id so this path cannot collide with
        # another generator that happens to share the seed.
        rng = np.random.default_rng([seed, 0x5058])
        z1 = rng.standard_normal(n)
        z2 = RHO*z1 + math.sqrt(max(1.0 - RHO*RHO, 0.0))*rng.standard_normal(n)
        S = np.empty(n + 1)
        v = np.empty(n + 1)
        S[0], v[0] = SPOT, V0
        sdt = math.sqrt(self.dt)
        mu = drift_r - drift_q
        for i in range(n):
            vp = max(v[i], 0.0)
            S[i+1] = S[i]*math.exp((mu - 0.5*vp)*self.dt + math.sqrt(vp)*sdt*z1[i])
            v[i+1] = v[i] + KAPPA*(THETA - vp)*self.dt + ETA*math.sqrt(vp)*sdt*z2[i]
        self.spot = S
        self.var = np.clip(v, 0.0, None)

    def elapsed_years(self, index: int) -> float:
        return index*self.dt

    def state(self, index: int) -> tuple[float, float, float]:
        i = min(max(index, 0), len(self.spot) - 1)
        return float(self.spot[i]), float(self.var[i]), self.elapsed_years(i)


class SPXMovingSurface(SPXHestonSurface):
    """`SPXHestonSurface`, re-priced from an arbitrary (S, v, elapsed) state.

    The parent prices the calibration snapshot: v = v0, tau = T, F and D as
    given. This one re-derives all four from the state, which is what makes the
    surface move.
    """

    def density_for(self, tau: float, v: float) -> tuple:
        """`(terminal_prices_over_forward, probabilities)` for maturity `tau`
        at current variance `v`.

        Returned relative to the forward -- the caller multiplies -- so the same
        density serves any spot at the same (tau, v). That is what makes
        caching on a quantised state worth anything.
        """
        s = self.spec
        total = THETA*tau + (v - THETA)*(1.0 - math.exp(-KAPPA*tau))/KAPPA
        sd = math.sqrt(max(total, 1e-12))
        a, b = -s.cos_width*sd, s.cos_width*sd
        k = np.arange(s.cos_terms)
        uk = k*np.pi/(b - a)
        coef = np.real(self._cf_v(uk, tau, v)*np.exp(-1j*uk*a))
        coef[0] *= 0.5
        y = np.linspace(a, b, s.grid_points)
        f = np.clip((2.0/(b - a))*(np.cos(uk[None, :]*(y[:, None] - a)) @ coef), 0.0, None)
        dy = y[1] - y[0]
        mass = f.sum()*dy
        if mass <= 0:
            raise ValueError("the COS density collapsed; widen cos_width")
        return np.exp(y), f*dy/mass

    @staticmethod
    def _cf_v(u, T: float, v: float):
        """The parent's characteristic function with v0 replaced by `v`."""
        u = np.asarray(u, dtype=complex)
        xi = KAPPA - 1j*RHO*ETA*u
        d = np.sqrt(xi**2 + (ETA**2)*(u**2 + 1j*u))
        g = (xi - d)/(xi + d)
        e = np.exp(-d*T)
        C = (KAPPA*THETA/ETA**2)*((xi - d)*T - 2.0*np.log((1.0 - g*e)/(1.0 - g)))
        D = ((xi - d)/ETA**2)*((1.0 - e)/(1.0 - g*e))
        return np.exp(C + D*v)

    def strikes_for(self, forward: float) -> list[float]:
        s = self.spec
        atm = round(forward/s.near_spacing)*s.near_spacing
        near = s.near_halfwidth_frac*forward
        wide = s.wide_halfwidth_frac*forward
        out = {atm + j*s.near_spacing
               for j in range(-int(near/s.near_spacing), int(near/s.near_spacing) + 1)}
        kk = round((atm - wide)/s.wide_spacing)*s.wide_spacing
        while kk <= atm + wide:
            if abs(kk - atm) > near:
                out.add(kk)
            kk += s.wide_spacing
        return sorted(round(x, 6) for x in out if x > 0)

    def quotes_for_state(self, ts: int, session: int, spot: float, var: float,
                         elapsed: float) -> list[OptionQuote]:
        """The whole surface at one instant of the path.

        An expiry that has passed is not listed at all -- `tau <= 0` is not a
        wide quote, it is a contract that no longer exists. That is also what
        lets a run continue past an expiry: the surface simply gets shorter,
        and the engine settles the position at the close of the expiry session.
        """
        out: list[OptionQuote] = []
        for pidx, e, tau, r, q in self._listed(elapsed):
            fwd = spot*math.exp((r - q)*tau)
            disc = math.exp(-r*tau)
            rel, prob = self.density_for(tau, var)
            terminal = fwd*rel
            ks = self.strikes_for(fwd)
            k = np.asarray(ks, dtype=float)
            calls = disc*(np.maximum(terminal[None, :] - k[:, None], 0.0) @ prob)
            puts = calls - disc*(fwd - k)
            atm = min(range(len(ks)), key=lambda j: abs(ks[j] - fwd))
            shift: dict[tuple[float, bool], float] = {}
            for pl in self.spec.plants:
                if pl.expiry_index != pidx:
                    continue
                j = min(max(atm + pl.strike_offset, 0), len(ks) - 1)
                shift[(ks[j], True)] = (-pl.strength if pl.kind == "parity"
                                        else pl.strength)
            for j, strike in enumerate(ks):
                for is_call, mid in ((True, float(calls[j])), (False, float(puts[j]))):
                    mid = max(mid + shift.get((strike, is_call), 0.0), 0.0)
                    bid, ask = self._quote_at(mid, tau)
                    bn, an = self.sizes(mid, strike, fwd, tau)
                    if bid <= 0.0:
                        bn = 0   # a 0.00 bid is nobody bidding
                    out.append(OptionQuote(ts=ts, session=session,
                                           expiry_session=e,
                                           strike=float(strike), is_call=is_call,
                                           bid=bid, ask=ask, bid_size=bn, ask_size=an))
        return out

    def _listed(self, elapsed: float):
        """Every contract quoted right now: `(plant_index, expiry_session, tau,
        r, q)`, one tuple per listed expiry.

        Factored out so the rolling surface below can change WHICH contracts
        exist without touching how any of them is priced or quoted -- the
        pricing is the part with a bug history, and it should have exactly one
        implementation.

        `plant_index` is what `Plant.expiry_index` selects, and it is an index
        into `self.maturities` here: a plant on the calibration's third expiry
        stays on that contract for as long as the contract exists, and vanishes
        with it. The rolling surface means something different by it, and says
        so.
        """
        out = []
        for i, (e, m) in enumerate(zip(expiry_sessions(self.maturities),
                                       self.maturities)):
            tau = m.T - elapsed
            if tau > 1e-6:                   # an expiry that has passed is gone
                out.append((i, e, tau, m.r, m.q))
        return out

    def settles_within(self, n_sessions: int) -> tuple[int, ...]:
        """Expiries that fall inside a run of `n_sessions`, in session order.

        The dataset asks this to refuse a configuration where nothing can ever
        settle. It is a method rather than a comprehension over `maturities`
        because the rolling surface's expiries are not a list it holds.
        """
        return tuple(e for e in expiry_sessions(self.maturities)
                     if e < n_sessions)

    def _quote_at(self, mid: float, tau: float) -> tuple[float, float]:
        s = self.spec
        h = s.min_half_spread + s.half_spread_frac*mid + s.far_expiry_widening*tau
        inc = s.dime_increment if mid >= s.dime_above else s.nickel_increment
        bid = max(0.0, math.floor((mid - h)/inc)*inc)
        ask = math.ceil((mid + h)/inc)*inc
        return round(bid, 4), round(ask, 4)

    def forward_discount(self, i: int, elapsed: float, spot: float) -> tuple:
        m = self.maturities[i]
        tau = m.T - elapsed
        return spot*math.exp((m.r - m.q)*tau), math.exp(-m.r*tau), tau


class SPXHestonDataset(SyntheticDataset):
    """The Heston path as the underlying, the SPX surface as the options channel.

    **Subclassed rather than written fresh, for the third time and the same
    reason.** `SyntheticDataset` carries the calendar, the train/validation/
    holdout split, the embargo, session irregularity, early closes,
    `scored_sessions`, `warmup_sessions` and the holdout lock -- every one with
    a defect history behind it. Only the price path is replaced, so the
    underlying a strategy sees and the forward the options are priced from are
    the SAME path. That identity is the point: an options feed driven by one
    series while the strategy trades another is a dataset that cannot be
    arbitraged correctly even in principle.

    `uniform=True` is forced. The parent's irregular sessions are there to stop
    a strategy learning a session length, and they are right for that -- but the
    options state here is indexed by position in a uniform minute grid, and
    ragged sessions would put the surface and the path on different clocks.
    Stated rather than silently assumed.
    """

    def __init__(self, spec=None, surface: SPXMovingSurface | None = None,
                 path_seed: int = 0, allow_no_settlement: bool = False):
        from dataclasses import replace
        # 40 sessions is 0.1587 years against the front expiry's 0.1830, so
        # the run ends with roughly six sessions still to run on the nearest
        # option. The guard below rejects anything longer; 60 -- the parent's
        # usual default -- reaches past it, which is how this number was found.
        # The floor is about 20: below that the 60/20 split plus a 2-session
        # embargo does not fit, which the parent refuses on its own.
        base = spec or SyntheticSpec(n_sessions=40)
        # **A REAL TRADING CLOCK, and the reason is that hour-of-day was
        # meaningless without one.**
        #
        # Inherited from the synthetic generator, a session was 390 ticks at 30
        # seconds -- 3.25 hours -- followed by a 17-hour gap. That is a 20.25
        # hour cycle, so every session started 3h45m earlier than the last:
        # 22:13, 18:28, 14:43, and round the clock. "Hour of day" was then
        # arithmetic on the session index with no market content whatsoever,
        # and the robustness seat's per-hour concentration test was reading
        # that arithmetic. spx-arb-01 was vetoed on it.
        #
        # 390 bars of 60 seconds is 6.5 hours, which is exactly a regular
        # session, and a 17.5-hour gap makes the cycle exactly 24 hours so the
        # open does not drift. `_schedule` below anchors it to 09:30 New York.
        #
        # Set HERE rather than on `SyntheticSpec`: `SyntheticDataset.digest`
        # hashes every spec field, so a new field with a default would move the
        # digest of every synthetic dataset in the project and the golden set
        # with it. This class already rewrites its own spec, so these two
        # values change this dataset's digest and nothing else's -- which is
        # the intended consequence. A run on the drifting clock and a run on
        # this one are different experiments and their digests say so.
        base = replace(base, uniform=True, ticks_per_session=QUOTES_PER_SESSION,
                       tick_spacing_ns=SESSION_TICK_SPACING_NS,
                       session_gap_ns=SESSION_GAP_NS)
        super().__init__(base)
        # **Short-dated by default, and that is a correctness choice.** On
        # the calibrated expiries nothing in a forty-session run ever reaches
        # settlement, so an arbitrage in the surface is visible, executable and
        # permanently unprofitable -- the gate would reject a detector that was
        # working perfectly. `short_maturities()` prices the same calibrated
        # parameters at expiries that fall inside the window. Pass `surface=`
        # with `maturities=MATURITIES` to get the calibration's own expiries,
        # which is the right choice for validating the fit and the wrong one
        # for running a campaign.
        self.surface = surface or SPXMovingSurface(
            SPXSpec(cos_terms=256, grid_points=601),
            maturities=short_maturities())
        self.path = SPXHestonPath(n_sessions=base.n_sessions, seed=path_seed,
                                  quotes_per_session=QUOTES_PER_SESSION)
        # **This guard used to say the opposite, and was wrong.** It refused
        # any run that REACHED the front expiry, on the reasoning that there is
        # no mechanism for rolling into new contracts. True, and harmless: an
        # expiry that passes simply stops being listed (`quotes_for_state`),
        # and the engine settles the position at that session's close. What is
        # not harmless is a run where nothing expires, because settlement is
        # the only way a locked-in edge is ever paid -- so the condition is
        # inverted. Reaching an expiry is the requirement, not the hazard.
        exp = expiry_sessions(self.surface.maturities)
        self.settling_expiries = self.surface.settles_within(base.n_sessions)
        if not self.settling_expiries and not allow_no_settlement:
            raise ValueError(
                f"none of this surface's expiries falls inside a "
                f"{base.n_sessions}-session run: they are at sessions "
                f"{exp}. Nothing would ever settle, so a package bought at a "
                f"mispricing could only be closed at the same quotes it was "
                f"opened at -- the edge would be visible, executable and "
                f"permanently unprofitable, and the gate would read a working "
                f"detector as a failure. Use `short_maturities()`, which "
                f"prices the same calibrated parameters at expiries inside the "
                f"window, or pass allow_no_settlement=True if the run is for "
                f"detection only and no position is meant to be held.")
        # **`n_sessions` is in the name, because the name is what a replay
        # matches on.** It was not, so a 40-session surface and a 175-session
        # one built from the same spec were the same NAME and different
        # DIGESTS: `scripts/replay.py` found a config entry whose name matched,
        # rebuilt the wrong dataset, and refused on the digest -- reporting
        # "the data has changed" for what was really two datasets sharing an
        # identity. A name that does not distinguish is worse than no name.
        self.name = (f"spx-heston:{self.surface.name}:"
                     f"n={base.n_sessions}:path={path_seed}")
        self._ts_index: dict[int, int] = {}
        self._ts_sorted: list[int] | None = None
        self._feed = _SPXFeed(self)

    def _schedule(self) -> None:
        """The parent's schedule, shifted to open at 09:30 New York.

        The parent starts session 0 at `SESSION_ORIGIN_NS`, which is 22:13 UTC
        -- fine for a generator whose clock means nothing, useless for a
        surface whose per-hour behaviour an auditor is asked to judge. The
        cycle is already exactly 24 hours by the spec above, so a single
        constant shift shifts every session together and the open is 09:30 for
        all of them.

        Shifting here rather than parameterising the parent's origin, for the
        same reason the spacing is set in `__init__`: a new `SyntheticSpec`
        field would be hashed into every synthetic dataset's digest.
        """
        super()._schedule()
        shift = RTH_ORIGIN_NS - SESSION_ORIGIN_NS
        self._starts = [t + shift for t in self._starts]
        self._ends = [t + shift for t in self._ends]

    # ---- the underlying is the path --------------------------------------- #
    def _session_ticks(self, session: int) -> list[Tick]:
        hit = self._cache.get(session)
        if hit is not None:
            return hit
        start, _ = self.session_bounds(session)
        spacing = self.spec.tick_spacing_ns
        n = self._lengths[session]
        base = session*QUOTES_PER_SESSION
        out = []
        for j in range(n):
            idx = base + j
            ts = start + j*spacing
            self._ts_index[ts] = idx
            px = float(self.path.spot[min(idx, len(self.path.spot) - 1)])
            out.append(Tick(ts, round(px, 2), 1, session,
                            side=Side.BUY if j % 2 == 0 else Side.SELL))
        self._cache[session] = out
        return out

    def _sorted_ts(self) -> list[int]:
        """The instants produced so far, sorted, for the feed to bisect.

        Rebuilt when the index grows rather than on every call: a strategy
        reading the surface each bar would otherwise sort ten thousand
        timestamps per read.
        """
        if self._ts_sorted is None or len(self._ts_sorted) != len(self._ts_index):
            self._ts_sorted = sorted(self._ts_index)
        return self._ts_sorted

    # ---- the options channel ---------------------------------------------- #
    def capabilities(self) -> DataCapabilities:
        return DataCapabilities(has_trades=True, has_quotes=False,
                                has_book=False, has_options=True)

    def options(self) -> "_SPXFeed":
        return self._feed

    def digest(self) -> str:
        """Covers the path AND the surface: otherwise a run with a plant would
        be indistinguishable in the record from a clean one."""
        return hashlib.sha256(
            f"{super().digest()}|{self.surface.digest()}|"
            f"{self.path.seed}".encode()).hexdigest()[:16]


class SPXRollingSurface(SPXMovingSurface):
    """The same Heston pricing, on expiries that ROLL instead of running out.

    **Why this exists.** The calibration lists three real expiries, so a
    surface built on it has three contracts and then none. A forty-session run
    splits 0-23 train / 26-33 validation / 36-39 holdout, and a fixed ladder
    cannot put a settlement inside every window -- the short ladder
    (5/10/21/35) settles three times in train and NOT ONCE in validation. An
    edge that is only paid at settlement would then score on train and score
    zero on validation, and the gate would reject a detector that worked
    perfectly. That is a property of the data, not of the strategy, and it
    would have been invisible: the validation run completes, reports nothing,
    and looks like an honest failure to replicate.

    So expiries sit on a fixed grid of sessions and the surface lists the next
    few unexpired ones, which is what a listed options market actually does.
    Every window of the run then contains settlements, and the same question
    can be asked of each.

    This is the convention the Gaussian surface in `options.py` already used --
    its `expiries_in_sessions` are TENORS and it writes `session + expiry` --
    so the two surfaces now agree about this as well.

    **`Plant.expiry_index` means something different here, deliberately.** On
    the fixed surface it selects a contract and the plant dies with it. Here it
    selects a POSITION IN THE LADDER -- 0 is whichever contract is nearest
    expiry at the moment of the quote -- so the mispricing is a standing
    property of the front month rather than an event that happens once. A
    campaign needs the former: a plant that expires a third of the way through
    train leaves most of the run with nothing to find.

    A contract is quoted while it has more than one session to run and settles
    at the close of its expiry session, so there is a one-session window in
    which a position is held but not quoted. That is expiration day, and it is
    immaterial to a package held to settlement.
    """

    def __init__(self, spec: SPXSpec | None = None, *,
                 grid_sessions: int = 5, n_listed: int = 4,
                 anchor: Maturity | None = None):
        if grid_sessions < 1:
            raise ValueError("expiries must be at least one session apart")
        if n_listed < 1:
            raise ValueError("a surface with no listed expiry quotes nothing")
        # **Set before `super().__init__`, because the parent builds `name`
        # from `self.digest()` and this subclass's digest reads them.** The
        # other order raises AttributeError out of the base constructor, which
        # reads as a bug in the base class.
        self.grid_sessions = grid_sessions
        self.n_listed = n_listed
        # The ladder as it stands at session zero. Passed to the parent so that
        # plant validation, `r`/`q` and the digest all have something concrete
        # to work from; `_listed` recomputes it at every instant.
        super().__init__(spec, maturities=short_maturities(
            tuple(grid_sessions*(i + 1) for i in range(n_listed)), anchor))
        self.name = (f"spx-heston-rolling:plants={len(self.spec.plants)}:"
                     f"every={grid_sessions}x{n_listed}:{self.digest()[:8]}")

    def _listed(self, elapsed: float):
        """The next `n_listed` expiries strictly ahead of now.

        `e > now` rather than `e >= now`: a contract with zero time left has no
        density to price under, and `tau = 0` would divide the COS expansion by
        a zero standard deviation. It is also the truthful statement -- at the
        moment it expires it is not a contract, it is a settlement.
        """
        now = elapsed/SESSION_YEARS
        a = self.maturities[0]
        first = math.floor(now/self.grid_sessions) + 1
        return [
            (i, (first + i)*self.grid_sessions,
             ((first + i)*self.grid_sessions - now)*SESSION_YEARS, a.r, a.q)
            for i in range(self.n_listed)
        ]

    def settles_within(self, n_sessions: int) -> tuple[int, ...]:
        """Every grid expiry inside the run -- which is most of them."""
        g = self.grid_sessions
        return tuple(range(g, n_sessions, g))

    def digest(self) -> str:
        return hashlib.sha256(
            f"rolling|{self.grid_sessions}|{self.n_listed}|"
            f"{super().digest()}".encode()).hexdigest()[:16]


class _SPXFeed:
    """Serves the moving surface, indexed by the instant the engine is at.

    **Keyed on the timestamp, not on the price.** The Gaussian feed could key on
    the forward because its density had a constant variance, so the price was
    the whole state. Under Heston the density also depends on `v_t`, which is
    NOT observable to a strategy -- correctly, since implied vol is observable
    and the instantaneous variance of the process is not. So the feed resolves
    the engine's timestamp to a position on the path and reads the state there.

    No look-ahead is possible: the state at a timestamp is the path up to that
    timestamp, and a timestamp the walk has not reached is refused rather than
    extrapolated.
    """

    __slots__ = ("_ds", "_cache", "hits", "misses")

    def __init__(self, dataset: SPXHestonDataset):
        self._ds = dataset
        self._cache: dict[int, list[OptionQuote]] = {}
        self.hits = self.misses = 0

    def snapshot(self, ts: int, session: int, forward: float) -> list[OptionQuote]:
        """The state as of `ts`: the latest path instant AT OR BEFORE it.

        **Not an exact timestamp match, and the first version was.** A callback
        fires at a BAR CLOSE, which is not one of the tick timestamps the path
        produced, so exact matching refused every single read -- 144 of 144 on
        the first engine run. Resolving backwards is also the correct
        semantics rather than merely the working one: "as of this instant" means
        the most recent state the walk has reached, which is exactly what makes
        look-ahead impossible. A timestamp before the first tick is refused,
        because there is no state yet rather than a nearest one.
        """
        import bisect
        times = self._ds._sorted_ts()
        pos = bisect.bisect_right(times, ts) - 1
        if pos < 0:
            raise KeyError(
                f"no path state at or before ts {ts}: the walk has not produced "
                f"an instant yet. There is no nearest state to fall back to.")
        idx = self._ds._ts_index[times[pos]]
        hit = self._cache.get(idx)
        if hit is not None:
            self.hits += 1
            return hit
        self.misses += 1
        spot, var, elapsed = self._ds.path.state(idx)
        out = self._ds.surface.quotes_for_state(ts, session, spot, var, elapsed)
        if len(self._cache) > SNAPSHOT_CACHE:
            self._cache.clear()
        self._cache[idx] = out
        return out


# --------------------------------------------------------------------------- #
# Option assets, and expiries that fall inside a run.
# --------------------------------------------------------------------------- #
# **A permanent mispricing cannot be monetised by closing**, and that decides
# the shape of everything below. Buy the cheap call and sell it back at the same
# quote and you have paid the spread twice for nothing. A conversion is a
# synthetic bond: its locked-in edge appears at SETTLEMENT, not on a round trip.
#
# The calibrated expiries are 0.183, 1.18 and 4.19 years out, and a run of forty
# sessions is 0.159 -- so none of them ever expires and no profit could ever be
# realised. Filling options without reaching an expiry would produce an
# arbitrage that is visible, executable and permanently unprofitable, which
# would read as the loop failing rather than as the harness being unable to pay.
#
# So the surface also lists SHORT-dated expiries, generated from the same
# calibrated parameters. That is sound rather than convenient: Heston's
# parameters are maturity-independent by construction -- that is what makes it
# a stochastic-volatility model rather than a curve fit -- so the model prices
# any maturity. `r` and `q` come from the nearest calibrated point.
#
# Measured on the four listed below, 2,456 quotes:
#
#     sessions   T        ATM iv    95/105 skew    |phi(-i) - 1|
#      5         0.0198   14.25%      +7.63            0.0
#     10         0.0397   14.00%      +7.51            0.0
#     21         0.0833   13.68%      +7.18            0.0
#     35         0.1389   13.57%      +6.67            0.0
#
# ATM vol rises towards sqrt(v0) = 14.53% as maturity shortens, which is what
# the model says must happen; the skew flattens with maturity, likewise. The
# martingale condition phi(-i) = 1 holds exactly at every one. Parity (with
# discounting -- see `_parity_for`), vertical and butterfly all report ZERO
# violations. Calendar is not checked, and `violations()` explains why.
#
# **Four rather than three, and 35 rather than 46.** The run is forty sessions,
# so a ladder ending at 21 leaves nineteen sessions with no listed option at
# all -- half the run spent on a surface that has gone empty. Ending at 35
# keeps options quoted for seven-eighths of it while still settling inside the
# window, which is the whole point of the exercise.
# --------------------------------------------------------------------------- #
SHORT_SESSIONS = (5, 10, 21, 35)


def short_maturities(sessions: tuple[int, ...] = SHORT_SESSIONS,
                     anchor: Maturity | None = None) -> tuple[Maturity, ...]:
    """Expiries `sessions` sessions out, from the calibrated parameters."""
    a = anchor or MATURITIES[0]
    out = []
    for n in sessions:
        T = n/252.0
        out.append(Maturity(f"+{n}s", SPOT*math.exp((a.r - a.q)*T),
                            math.exp(-a.r*T), a.r, a.q))
    return tuple(out)


SPX_UNDERLYING = "SPX"


def option_asset(expiry_session: int, strike: float, is_call: bool) -> str:
    """This surface's options, named for the engine. See
    `options.option_asset` -- this binds the underlying, nothing more."""
    return _option_asset(SPX_UNDERLYING, expiry_session, strike, is_call)


def option_asset_of(q) -> str:
    """The name of the option `q` quotes, on this surface."""
    return _option_asset_of(SPX_UNDERLYING, q)
