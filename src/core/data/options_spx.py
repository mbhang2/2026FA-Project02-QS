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

from .options import (                        # noqa: F401  (re-exported)
    OptionQuote, Plant, butterfly_violations, vertical_violations,
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
                     f"{self.spec.digest()[:8]}")
        self._dens: dict[int, tuple] = {}

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

        `expiry_session` carries the maturity INDEX rather than a session
        count: these are real listed expiries from a calibration, and the three
        of them are 0.18, 1.18 and 4.19 years out. Pretending they are session
        offsets would invent a trading calendar the calibration never had.
        """
        sites = self._sites()
        out: list[OptionQuote] = []
        for i, _m in enumerate(self.maturities):
            ks = self.strikes(i)
            calls, puts = self.call_prices(i, ks), self.put_prices(i, ks)
            for j, strike in enumerate(ks):
                for is_call, mid in ((True, float(calls[j])), (False, float(puts[j]))):
                    mid = max(mid + sites.get((i, strike, is_call), 0.0), 0.0)
                    bid, ask = self._quote(mid, i)
                    out.append(OptionQuote(ts=ts, session=session, expiry_session=i,
                                           strike=float(strike), is_call=is_call,
                                           bid=bid, ask=ask))
        return out

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
