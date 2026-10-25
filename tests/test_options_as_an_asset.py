"""Options as a tradeable asset: naming, filling, size, and settlement.

`tests/test_options_through_the_engine.py` covers the READ seam -- that a
strategy can see the cross-section. This file covers the WRITE seam: that an
order can name one option out of several thousand, fill against its quoted
touch for no more than the quoted size, keep its own book, and be settled into
cash when the contract expires.

Settlement is the part that makes any of it worth having. A permanent
mispricing cannot be monetised by closing -- the quotes it was bought at are
the quotes it would be sold back at -- so a locked-in edge is paid at expiry or
never. The box-spread test at the bottom is the one that proves the whole
chain, and it is the shape of an answer to the options-arbitrage challenge.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

from src.core.data.options import (                              # noqa: E402
    OptionQuote, Plant, SyntheticOptionsDataset, SyntheticOptionsSpec,
    SyntheticOptionsSurface, is_option_asset, option_asset, option_asset_of,
    parity_violations, parse_option_asset,
)
from src.core.data.synthetic import SyntheticDataset, SyntheticSpec   # noqa: E402
from src.core.engine import Engine                               # noqa: E402
from src.core.engine.context import ContextError                 # noqa: E402
from src.core.types import (                                     # noqa: E402
    DEFAULT_ASSET, LifecycleKind, OrderType, Split, TimeInForce,
)

SMALL = dict(start_price=100.0, vol_per_session=1.0,
             near_halfwidth=8.0, wing_halfwidth=20.0)
U = "QQQ"


def _dataset(plants=()):
    u = SyntheticDataset(SyntheticSpec(n_sessions=60, uniform=True))
    return SyntheticOptionsDataset(
        u, SyntheticOptionsSurface(SyntheticOptionsSpec(plants=plants, **SMALL)))


def _run(strategy, dataset=None):
    eng = Engine.for_test(dataset or _dataset(), strategy, split=Split.TRAIN,
                          fill_model="unfavorable", seed=3)
    return eng, eng.run()


class _Once:
    """Places one order on one option, the first time it sees the surface."""

    pick = staticmethod(lambda q: q.is_call and q.ask_size > 0)
    side = "buy"
    kw: dict = {}

    def setup(self, ctx):
        ctx.subscribe("5m")
        self.done = False
        self.quote = None
        self.asset = None
        self.fills: list = []
        self.rejects: list = []
        self.err = None

    def choose(self, ctx, surface):
        ok = [q for q in surface if self.pick(q)]
        return min(ok, key=lambda c: abs(c.strike - ctx.last_price)) if ok else None

    def on_bars(self, ctx):
        try:
            if self.done:
                return
            q = self.choose(ctx, list(ctx.options()))
            if q is None:
                return
            self.quote, self.asset = q, option_asset_of(U, q)
            send = ctx.buy if self.side == "buy" else ctx.sell
            send(self.size(q), asset=self.asset, **self.kw)
            self.done = True
        except Exception as exc:                                 # noqa: BLE001
            self.err = self.err or f"{type(exc).__name__}: {exc}"

    def size(self, q):
        return 1

    def on_fill(self, ctx):
        self.fills.append((ctx.fill.asset, ctx.fill.side.value, ctx.fill.price,
                           ctx.fill.size, ctx.fill.position, ctx.position))

    def on_lifecycle(self, ctx):
        if ctx.event.detail:
            self.rejects.append((ctx.event.kind, ctx.event.detail))


# --------------------------------------------------------------------------- #
# naming
# --------------------------------------------------------------------------- #
def test_a_name_round_trips_and_a_non_option_is_not_one():
    name = option_asset("SPX", 7849, 7850.0, True)
    assert name == "SPX-7849-C-7850", "a whole strike must not acquire a .0"
    assert parse_option_asset(name) == (7849, 7850.0, True)
    assert is_option_asset(name)

    assert parse_option_asset(DEFAULT_ASSET) is None, (
        "the engine asks this of every asset it is handed, and the default "
        "asset is not an option")
    for junk in ("", "SPX", "SPX-x-C-7850", "SPX-1-X-7850", "SPX-1-C-nope",
                 "SPX-1-C-7850-extra"):
        assert parse_option_asset(junk) is None, junk


def test_a_name_comes_from_the_quote_it_refers_to():
    q = OptionQuote(ts=1, session=4, expiry_session=11, strike=99.5,
                    is_call=False, bid=1.0, ask=1.2, bid_size=3, ask_size=4)
    name = option_asset_of(U, q)
    assert name == "QQQ-11-P-99.5"
    assert parse_option_asset(name) == (q.expiry_session, q.strike, q.is_call)


def test_an_underlying_with_a_hyphen_is_refused():
    with pytest.raises(ValueError, match="hyphen"):
        option_asset("SP-X", 1, 100.0, True)
    with pytest.raises(ValueError, match="hyphen-free"):
        option_asset("", 1, 100.0, True)


def test_the_name_identifies_the_expiry_by_session_not_by_position():
    """A contract's name must mean the same thing for its whole life.

    Naming the front month `-0-` would re-point at a different contract the
    moment the front month rolled, so a position opened in one instrument
    would silently become a position in another.
    """
    d = _dataset()
    surf = d.options().snapshot(*_first_instant(d))
    for q in surf:
        assert parse_option_asset(option_asset_of(U, q))[0] == q.expiry_session


def _first_instant(d):
    t = next(iter(d.ticks(Split.TRAIN)))
    return t.ts, t.session, t.price


def _settlement_detail(events) -> tuple[float, float]:
    """`(underlying_at_expiry, intrinsic)` out of the expiry announcement.

    The engine says it in words -- "expired with the underlying at X: settled
    +N at intrinsic Y" -- and that message is the only place a strategy can see
    the price the contract was settled against, so a test that wants to check
    the arithmetic reads it from there rather than guessing at a tick.
    """
    import re
    for kind, detail in events:
        m = re.search(r"underlying at ([\d.]+).*intrinsic ([\d.]+)", detail)
        if m:
            return float(m.group(1)), float(m.group(2))
    raise AssertionError(f"no settlement announcement in {events}")


# --------------------------------------------------------------------------- #
# the fill
# --------------------------------------------------------------------------- #
def test_an_option_order_fills_at_the_touch_in_its_own_book():
    st = _Once()
    eng, rec = _run(st)
    assert rec.outcome == "completed" and st.err is None, st.err
    assert st.fills, "the order never filled"

    asset, side, price, size, pos, ctx_pos = st.fills[0]
    assert asset == st.asset
    assert side == "buy" and size == 1
    assert price == st.quote.ask, (
        f"a buy must pay the offer: filled {price}, offered {st.quote.ask}")
    # The position is read AT THE FILL, not at the end of the run: the contract
    # expires inside the run and settlement flattens the book, which is the
    # whole point of the settlement tests below.
    assert pos == 1
    assert eng.position_of(DEFAULT_ASSET) == 0, (
        "the underlying must not move because an option traded")
    assert eng.blotter.rows_for(st.asset), "the fill is not in the blotter"
    assert eng.blotter.rows_for(DEFAULT_ASSET) == [], (
        "an option fill must not land in the underlying's book")


def test_a_sell_hits_the_bid():
    class S(_Once):
        side = "sell"
        pick = staticmethod(lambda q: q.is_call and q.bid_size > 0 and q.bid > 0)
    st = S()
    eng, rec = _run(st)
    assert st.err is None and st.fills, st.err
    assert st.fills[0][2] == st.quote.bid
    assert st.fills[0][4] == -1, "the short is in the option's own book"
    assert eng.position_of(DEFAULT_ASSET) == 0


def test_the_fill_is_capped_by_the_quoted_size():
    class S(_Once):
        kw = {"tif": TimeInForce.IOC}

        def size(self, q):
            return q.ask_size + 50
    st = S()
    eng, rec = _run(st)
    assert st.err is None and st.fills, st.err
    assert st.fills[0][3] == st.quote.ask_size, (
        f"took {st.fills[0][3]} against a quoted {st.quote.ask_size}")
    entries = [f for f in st.fills if f[1] == "buy"]
    assert len(entries) == 1, (
        f"IOC must cancel the remainder rather than keep working: {entries}")
    assert st.fills[0][4] == st.quote.ask_size


def test_a_quote_with_no_size_cannot_be_hit():
    """The rule the arbitrage checkers already enforce: an edge whose side is
    empty is not an edge, and a fill you could not have got is worse.

    IOC, and the FAR wing. A DAY order on a zero-bid quote is not a bug if it
    fills later -- the surface moves, and a strike whose bid rounded to nothing
    can be worth bidding for two ticks on. The first version of this test used
    the default DAY and caught exactly that, filling at a real bid some ticks
    after it was placed. One shot at the instant the bid is empty is the thing
    worth asserting.
    """
    class S(_Once):
        side = "sell"
        kw = {"tif": TimeInForce.IOC}
        pick = staticmethod(lambda q: q.bid_size == 0 and q.is_call)

        def choose(self, ctx, surface):
            ok = [q for q in surface if self.pick(q)]
            # the highest strike: the furthest from any chance of a bid
            return max(ok, key=lambda c: c.strike) if ok else None
    st = S()
    eng, rec = _run(st)
    assert st.err is None, st.err
    assert st.asset is not None, "no zero-size quote on the surface to test"
    assert st.quote.bid_size == 0 and st.quote.bid == 0.0
    assert not st.fills, f"filled against a quote with no size: {st.fills}"
    assert eng.blotter.rows_for(st.asset) == []


def test_a_limit_does_not_reach_through_the_touch_but_fills_when_it_does():
    class Through(_Once):
        def on_bars(self, ctx):
            try:
                if self.done:
                    return
                q = self.choose(ctx, list(ctx.options()))
                if q is None:
                    return
                self.quote, self.asset = q, option_asset_of(U, q)
                # A penny under the offer, good for the session.
                ctx.buy(1, asset=self.asset, type=OrderType.LIMIT,
                        limit=q.ask - 0.01, tif=TimeInForce.IOC)
                self.done = True
            except Exception as exc:                             # noqa: BLE001
                self.err = self.err or f"{type(exc).__name__}: {exc}"
    st = Through()
    _eng, _rec = _run(st)
    assert st.err is None, st.err
    assert not st.fills, "a limit under the offer must not be filled at the offer"

    class At(Through):
        def on_bars(self, ctx):
            try:
                if self.done:
                    return
                q = self.choose(ctx, list(ctx.options()))
                if q is None:
                    return
                self.quote, self.asset = q, option_asset_of(U, q)
                ctx.buy(1, asset=self.asset, type=OrderType.LIMIT,
                        limit=q.ask + 0.50, tif=TimeInForce.IOC)
                self.done = True
            except Exception as exc:                             # noqa: BLE001
                self.err = self.err or f"{type(exc).__name__}: {exc}"
    st2 = At()
    _eng, _rec = _run(st2)
    assert st2.err is None and st2.fills, st2.err
    assert st2.fills[0][2] <= st2.quote.ask + 1e-9, (
        "a marketable limit pays the touch, never its own worse limit")


def test_a_stop_on_an_option_is_refused_with_a_reason():
    class S(_Once):
        kw = {"type": OrderType.STOP, "trigger": 999.0, "tif": TimeInForce.GTC}
    st = S()
    _eng, rec = _run(st)
    assert rec.outcome == "completed" and st.err is None, st.err
    assert not st.fills
    said = [d for k, d in st.rejects if k is LifecycleKind.ORDER_REJECTED]
    assert any("stop" in d and "touch" in d for d in said), said


# --------------------------------------------------------------------------- #
# whose book
# --------------------------------------------------------------------------- #
def test_in_a_fill_callback_position_means_the_asset_that_filled():
    """Two numbers called "position" in one callback must not disagree."""
    st = _Once()
    _eng, _rec = _run(st)
    assert st.fills
    _asset, _side, _px, _n, event_pos, ctx_pos = st.fills[0]
    assert event_pos == ctx_pos == 1


def test_close_sizes_off_the_asset_it_is_closing():
    """`close` took **kw -- so it accepted `asset=` -- while sizing itself off
    the default book. Long the underlying and short an option, it would have
    sent the underlying's size against the option's position."""
    class S:
        def setup(self, ctx):
            ctx.subscribe("5m")
            self.stage = 0
            self.asset = None
            self.err = None
            self.closed = None

        def on_bars(self, ctx):
            try:
                if self.stage == 0:
                    ctx.buy(5)                      # five of the underlying
                    self.stage = 1
                elif self.stage == 1 and ctx.position == 5:
                    q = [x for x in ctx.options() if x.is_call and x.ask_size > 1]
                    if not q:
                        return
                    pick = min(q, key=lambda c: abs(c.strike - ctx.last_price))
                    self.asset = option_asset_of(U, pick)
                    ctx.buy(2, asset=self.asset)
                    self.stage = 2
                elif self.stage == 2 and ctx.position_in(self.asset) == 2:
                    self.closed = ctx.close(asset=self.asset)
                    self.stage = 3
            except Exception as exc:                             # noqa: BLE001
                self.err = self.err or f"{type(exc).__name__}: {exc}"

        def on_fill(self, ctx): pass
        def on_lifecycle(self, ctx): pass

    st = S()
    eng, _rec = _run(st)
    assert st.err is None, st.err
    assert st.stage == 3, f"never got to the close (stage {st.stage})"
    assert eng.position_of(st.asset) == 0, "the option book did not flatten"
    assert eng.position_of(DEFAULT_ASSET) == 5, (
        "closing the option must not touch the underlying")


def test_closing_a_book_that_is_flat_keeps_its_documented_message():
    """`tests/test_examples.py` asserts this string and
    `docs/agent/strategy-api.md` tables it, and that doc is fingerprinted into
    every proposal prompt."""
    class S:
        def setup(self, ctx):
            ctx.subscribe("5m")
            self.msgs: list[str] = []

        def on_bars(self, ctx):
            if len(self.msgs) < 2:
                for asset in (None, "QQQ-99-C-100"):
                    try:
                        ctx.close() if asset is None else ctx.close(asset=asset)
                    except ContextError as exc:
                        self.msgs.append(str(exc))

        def on_fill(self, ctx): pass
        def on_lifecycle(self, ctx): pass

    st = S()
    _run(st)
    assert st.msgs[0] == "close() with no position open"
    assert st.msgs[1] == "close() with no position open in QQQ-99-C-100"


# --------------------------------------------------------------------------- #
# settlement
# --------------------------------------------------------------------------- #
class _HoldToExpiry(_Once):
    """Buys the nearest-expiry call at a chosen strike and holds."""

    strike_rank = 0          # 0 = lowest listed strike, deep in the money

    def choose(self, ctx, surface):
        calls = [q for q in surface if q.is_call and q.ask_size > 0]
        if not calls:
            return None
        near = min(q.expiry_session for q in calls)
        front = sorted((q for q in calls if q.expiry_session == near),
                       key=lambda q: q.strike)
        return front[self.strike_rank]

    def on_lifecycle(self, ctx):
        super().on_lifecycle(ctx)


def test_a_call_that_expires_in_the_money_settles_at_intrinsic():
    st = _HoldToExpiry()
    eng, rec = _run(st)
    assert rec.outcome == "completed" and st.err is None, st.err
    assert len(st.fills) == 2, f"expected an entry and a settlement: {st.fills}"

    rows = eng.blotter.rows_for(st.asset)
    entry, settle = rows[0], rows[1]
    assert settle.tag == "settlement"
    expiry, strike, is_call = parse_option_asset(st.asset)
    assert is_call and settle.side.value == "sell"
    assert eng.position_of(st.asset) == 0, "the book did not flatten at expiry"

    # **Against the underlying's price at EXPIRY, not at the end of the run.**
    # The contract expires early and the run continues for another thirty-odd
    # sessions, so `eng.last_price` is a different number entirely -- the first
    # version of this test compared against it and failed by 0.75.
    underlying, announced = _settlement_detail(st.rejects)
    assert settle.price == pytest.approx(announced, abs=1e-9)
    assert settle.price == pytest.approx(max(underlying - strike, 0.0), abs=1e-9)
    assert settle.price > 0.0, "the lowest listed strike should finish ITM"
    assert settle.realized_pnl == pytest.approx(
        (settle.price - entry.price)*entry.size, abs=1e-9)


def test_a_call_that_expires_worthless_settles_at_zero():
    class S(_HoldToExpiry):
        strike_rank = -1     # the highest listed strike, far out of the money
    st = S()
    eng, rec = _run(st)
    assert st.err is None and len(st.fills) == 2, st.fills
    _expiry, strike, _c = parse_option_asset(st.asset)
    settle = eng.blotter.rows_for(st.asset)[1]
    assert strike > eng.last_price, "the highest strike should finish OTM"
    assert settle.price == 0.0
    assert settle.realized_pnl < 0.0, "a worthless long call must book its premium"


def test_settlement_is_in_the_fill_log_because_that_is_what_the_score_reads():
    """The score is reconstructed from the fill log by a pure function. A
    settlement booked anywhere else would be invisible to the gate, and on a
    conversion the entire edge appears at settlement."""
    st = _HoldToExpiry()
    eng, _rec = _run(st)
    settled = [f for f in eng.fill_log if f.tag == "settlement"]
    assert len(settled) == 1
    assert settled[0].asset == st.asset
    recon = eng.reconstructed()
    assert recon.realized_pnl_of(st.asset) == pytest.approx(
        eng.blotter.realized_pnl_of(st.asset), abs=1e-9)


def test_expiry_announces_itself_and_kills_working_orders_on_the_contract():
    class S(_HoldToExpiry):
        def on_bars(self, ctx):
            super().on_bars(ctx)
            # a resting order on the same contract, far from the touch
            if self.asset and not getattr(self, "rested", False):
                ctx.buy(1, asset=self.asset, type=OrderType.LIMIT,
                        limit=0.01, tif=TimeInForce.GTC)
                self.rested = True
    st = S()
    eng, rec = _run(st)
    assert rec.outcome == "completed" and st.err is None, st.err
    said = [d for k, d in st.rejects if k is LifecycleKind.CONTRACT_EXPIRED]
    assert any("expired with the underlying at" in d for d in said), said
    assert all(o.asset != st.asset for o in eng._working.values()), (
        "an order was left resting on a contract that no longer exists")


def test_an_order_on_an_already_expired_contract_is_refused():
    class S(_Once):
        def on_bars(self, ctx):
            try:
                if getattr(self, "fired", False):
                    return
                surf = list(ctx.options())
                if not surf or ctx.session < 6:
                    return
                # a contract whose expiry is already behind us
                self.asset = option_asset(U, 1, 100.0, True)
                ctx.buy(1, asset=self.asset, tif=TimeInForce.GTC)
                self.fired = True
            except Exception as exc:                             # noqa: BLE001
                self.err = self.err or f"{type(exc).__name__}: {exc}"
    st = S()
    _eng, rec = _run(st)
    assert rec.outcome == "completed" and st.err is None, st.err
    assert not st.fills
    said = [d for k, d in st.rejects if k is LifecycleKind.CONTRACT_EXPIRED]
    assert any("expired at session 1" in d for d in said), said


# --------------------------------------------------------------------------- #
# the whole chain
# --------------------------------------------------------------------------- #
def test_a_planted_parity_violation_is_monetised_by_a_box_held_to_expiry():
    """The test this file exists for.

    A box spread is long the synthetic underlying at one strike and short it at
    another: four option legs, no futures leg, and a payoff at expiry of
    exactly the difference between the strikes. That makes it the one package
    whose edge is realised entirely through settlement, with no overnight risk
    in the hedge and no second instrument's multiplier to reconcile.

    If the engine fills each leg at its own touch, keeps four separate books,
    and settles each at intrinsic, then the realised total must equal the
    difference between what the box paid out and what it cost -- to the penny,
    net of commission. That single number exercises every part of stage 6.
    """
    class Boxer:
        def setup(self, ctx):
            ctx.subscribe("5m")
            self.done = False
            self.plan = None
            self.err = None

        def on_bars(self, ctx):
            try:
                if self.done:
                    return
                surf = list(ctx.options())
                found = parity_violations(surf, ctx.last_price) if surf else []
                if not found:
                    return
                hit = max(found, key=lambda r: r["edge"])
                same = [q for q in surf if q.expiry_session == hit["expiry_session"]]
                pairs: dict = {}
                for q in same:
                    pairs.setdefault(q.strike, {})[q.is_call] = q
                usable = [k for k, d in pairs.items()
                          if k != hit["strike"] and len(d) == 2
                          and min(d[True].bid_size, d[True].ask_size,
                                  d[False].bid_size, d[False].ask_size) > 0]
                if not usable:
                    return
                other = min(usable, key=lambda k: abs(k - hit["strike"]))
                a, b = pairs[hit["strike"]], pairs[other]
                # long the synthetic where parity says it is cheap
                if hit["side"] == "long_via_options":
                    lo_k, lo, hi_k, hi = hit["strike"], a, other, b
                else:
                    lo_k, lo, hi_k, hi = other, b, hit["strike"], a
                cost = (lo[True].ask - lo[False].bid) - (hi[True].bid - hi[False].ask)
                self.plan = {"cost": cost, "payoff": hi_k - lo_k,
                             "net": (hi_k - lo_k) - cost, "edge": hit["edge"]}
                for q, buy in ((lo[True], True), (lo[False], False),
                               (hi[True], False), (hi[False], True)):
                    send = ctx.buy if buy else ctx.sell
                    send(1, asset=option_asset_of(U, q), tif=TimeInForce.IOC,
                         tag="box")
                self.done = True
            except Exception as exc:                             # noqa: BLE001
                self.err = self.err or f"{type(exc).__name__}: {exc}"

        def on_fill(self, ctx): pass
        def on_lifecycle(self, ctx): pass

    st = Boxer()
    eng, rec = _run(st, _dataset(plants=(Plant("parity", 1, 0, 0.60),)))
    assert rec.outcome == "completed" and st.err is None, st.err
    assert st.plan is not None, "no parity violation was ever seen"

    legs = [a for a in eng.blotter.assets() if a != DEFAULT_ASSET]
    assert len(legs) == 4, f"a box is four legs, got {legs}"
    assert all(eng.position_of(a) == 0 for a in legs), (
        "every leg must be flat once the contract has settled")
    assert sum(len(eng.blotter.rows_for(a)) for a in legs) == 8, (
        "four entries and four settlements")

    # `BlotterRow.realized_pnl` is GROSS -- commission is its own column, and
    # `_RunState.realized_pnl` is the one that nets them. Adding the fees back
    # here double-counted them and made a correct 0.45 read as 0.53.
    gross = sum(eng.blotter.realized_pnl_of(a) for a in legs)
    fees = sum(eng.blotter.commission_of(a) for a in legs)
    assert gross == pytest.approx(st.plan["net"], abs=1e-9), (
        f"the box realised {gross} against a locked-in {st.plan['net']}")
    assert st.plan["net"] > 0.0, "the plant should leave a positive locked edge"
    assert gross - fees > 0.0, "and it should survive the commission"

    # the reconstruction -- what the SCORER reads -- must agree leg by leg
    recon = eng.reconstructed()
    for a in legs:
        assert recon.realized_pnl_of(a) == pytest.approx(
            eng.blotter.realized_pnl_of(a), abs=1e-9), a
