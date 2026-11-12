"""Round turn trade Monte Carlo: blotter's `txnsim`, scored by the gate.

`src/core/scoring/randomization/txnsim.py` ports `blotter::txnsim` with
`tradeDef = 'flat.to.flat'`. These tests pin the port to blotter's semantics
where it follows them, to the stated rule where it departs (the trading clock,
overnight holding, single asset), and the fast scorer to the gate's own path.
"""

from __future__ import annotations

import numpy as np
import pytest

from src.core.engine.reconstruct import reconstruct
from src.core.scoring import metrics
from src.core.scoring.randomization import txnsim as tx
from src.core.scoring.scorer import score_returns
from src.core.types import FillRecord, Side

NS = 1_000_000_000
HOUR = 3600 * NS
COMM = 0.10


def _calendar(n):
    """`n` sessions of 8 hours, 16 hours apart -- a gap the clock must remove."""
    return [metrics.SessionSpan(100 + s, s * 24 * HOUR, s * 24 * HOUR + 8 * HOUR)
            for s in range(n)]


def _tape(cal, seed=0, per_session=400):
    rng = np.random.default_rng(seed)
    ts, px = [], []
    for span in cal:
        t = np.sort(rng.integers(span.start_ts, span.end_ts, size=per_session))
        t[0] = span.start_ts
        ts.append(t)
        px.append(100.0 + np.cumsum(rng.normal(0, 0.5, size=per_session)))
    return tx.PriceTape(ts, px)


def _fills(spec, tape, cal):
    """`spec` is [(ts, signed qty), ...]; each fill prices off the tape."""
    clock = tx.TradingClock(cal)
    out = []
    for seq, (ts, q) in enumerate(spec):
        i = clock.index_of(ts)
        fts, px = tape.at(i, ts)
        out.append(FillRecord(seq=seq, ts=fts, session=cal[i].session,
                              order_id=seq + 1,
                              side=Side.BUY if q > 0 else Side.SELL,
                              size=abs(q), price=px, liquidity="taker"))
    return out


def _blotter(spec, tape, cal):
    return reconstruct(_fills(spec, tape, cal), commission_per_contract=COMM)


def _layered(cal, rng, n=80):
    """Intraday round turns that add, scale out, and sometimes flip."""
    out = []
    for s in sorted(rng.choice(len(cal), size=min(n, len(cal)), replace=False)):
        a = cal[int(s)].start_ts + HOUR
        d = int(rng.choice([-1, 1]))
        kind = int(rng.integers(0, 3))
        if kind == 0:     # add, then scale out in two steps
            out += [(a, d), (a + 10 * 60 * NS, d), (a + 40 * 60 * NS, -d),
                    (a + 70 * 60 * NS, -d)]
        elif kind == 1:   # flip through zero, then flatten
            out += [(a, 2 * d), (a + 30 * 60 * NS, -3 * d), (a + 90 * 60 * NS, d)]
        else:             # one lot in, one lot out
            out += [(a, d), (a + 50 * 60 * NS, -d)]
    return out


def _intraday(cal, rng, n=120):
    """Round turns that open and close inside one session, one lot or two."""
    spec = []
    for _ in range(n):
        span = cal[int(rng.integers(0, len(cal)))]
        a = int(rng.integers(span.start_ts + NS, span.end_ts - 2 * HOUR))
        spec.append((a, int(rng.choice([-2, -1, 1, 2]))))
    spec.sort()
    out, last_end = [], -1
    for a, q in spec:
        if a <= last_end:
            continue
        b = a + int(rng.integers(NS, HOUR))
        out += [(a, q), (b, -q)]
        last_end = b
    return out


# --------------------------------------------------------------------------- #
# the stylized facts: perTradeStats(flat.to.flat, includeFlatPeriods = TRUE)
# --------------------------------------------------------------------------- #

def test_facts_are_round_turns_and_flat_periods_on_the_trading_clock():
    cal = _calendar(3)
    tape = _tape(cal)
    s0, s1 = cal[0].start_ts, cal[1].start_ts
    spec = [(s0 + 1 * HOUR, 2), (s0 + 2 * HOUR, -2),          # long 2, 1h
            (s0 + 3 * HOUR, -1), (s0 + 5 * HOUR, 1),          # short 1, 2h
            (s1 + 1 * HOUR, 1), (s1 + 1 * HOUR + NS, -1)]     # next session
    facts = tx.trade_facts(_blotter(spec, tape, cal), cal)
    q = [p.quantity for p in facts.periods]
    # The initial flat period is dropped and the trailing one is kept.
    assert q == [2, 0, -1, 0, 1, 0]
    assert facts.round_turns == 3
    clock = tx.TradingClock(cal)
    assert facts.first_start == clock.clock(_fills(spec, tape, cal)[0].ts)
    # Durations are trading time: the flat period from 05:00 of session 0 to
    # 01:00 of session 1 is 3h + 1h, not 20h. Fills sit on the trade at or
    # before the instant, so allow the tape's spacing either side.
    flat = facts.periods[3].duration
    assert abs(flat - 4 * HOUR) < HOUR / 4
    # Every period ends where the next begins, and the series ends at the
    # clock's end: nothing is double counted and nothing is lost.
    assert facts.first_start + sum(p.duration for p in facts.periods) == clock.total
    assert not facts.holds_overnight and not facts.layered


def test_a_flip_through_zero_does_not_end_a_round_turn_and_adds_are_layering():
    cal = _calendar(2)
    tape = _tape(cal)
    s = cal[0].start_ts
    spec = [(s + HOUR, 1), (s + 2 * HOUR, -2), (s + 3 * HOUR, 1),   # flip, one turn
            (s + 4 * HOUR, 1), (s + 4 * HOUR + NS, 1), (s + 5 * HOUR, -2)]
    facts = tx.trade_facts(_blotter(spec, tape, cal), cal)
    assert [p.quantity for p in facts.periods if p.quantity] == [1, 1]
    assert facts.layered            # +1 then +1 more: blotter keeps Init.Qty = 1


def test_an_overnight_hold_is_recognised():
    cal = _calendar(3)
    tape = _tape(cal)
    spec = [(cal[0].start_ts + HOUR, 1), (cal[1].start_ts + HOUR, -1)]
    assert tx.trade_facts(_blotter(spec, tape, cal), cal).holds_overnight


def test_a_position_still_open_at_the_end_is_not_a_round_turn():
    cal = _calendar(2)
    tape = _tape(cal)
    s = cal[0].start_ts
    spec = [(s + HOUR, 1), (s + 2 * HOUR, -1), (s + 3 * HOUR, 1)]
    facts = tx.trade_facts(_blotter(spec, tape, cal), cal)
    assert [p.quantity for p in facts.periods] == [1, 0]
    assert facts.round_turns == 1


def test_a_multi_asset_blotter_is_refused():
    cal = _calendar(1)
    tape = _tape(cal)
    fills = _fills([(cal[0].start_ts + HOUR, 1), (cal[0].start_ts + 2 * HOUR, -1)],
                   tape, cal)
    other = [FillRecord(seq=10 + f.seq, ts=f.ts, session=f.session,
                        order_id=10 + f.order_id, side=f.side, size=f.size,
                        price=f.price, liquidity="taker", asset="ES")
             for f in fills]
    b = reconstruct(fills + other, commission_per_contract=COMM)
    with pytest.raises(ValueError, match="single-asset"):
        tx.trade_facts(b, cal)


# --------------------------------------------------------------------------- #
# the sampling: idxexpr.wr and idxexpr.nr
# --------------------------------------------------------------------------- #

def _facts(seed=1, n_sessions=6):
    cal = _calendar(n_sessions)
    tape = _tape(cal, seed)
    b = _blotter(_intraday(cal, np.random.default_rng(seed)), tape, cal)
    return cal, tape, b, tx.trade_facts(b, cal)


def test_with_replacement_the_total_duration_is_the_observed_total_exactly():
    _, _, _, facts = _facts()
    target = sum(p.duration for p in facts.periods)
    rng = np.random.default_rng(3)
    for _ in range(50):
        rows = tx._sample_with_replacement(facts.periods, rng)
        assert sum(p.duration for p in rows) == target
        # Only the last row may have been trimmed; every other row is one of
        # the observed rows as it was.
        observed = set(facts.periods)
        assert all(p in observed for p in rows[:-1])


def test_without_replacement_the_rows_are_a_permutation():
    _, _, _, facts = _facts()
    rows = tx._sample_without_replacement(facts.periods, np.random.default_rng(4))
    assert sorted(rows, key=lambda p: (p.duration, p.quantity)) == \
        sorted(facts.periods, key=lambda p: (p.duration, p.quantity))


def test_replicates_start_at_the_first_observed_opening():
    cal, _, _, facts = _facts()
    clock = tx.TradingClock(cal)

    class Identity:                 # rows in their observed order
        def permutation(self, n):
            return np.arange(n)

    trades = tx.replicate(facts, clock, Identity(), replacement=False,
                          mode="flat.to.flat")
    assert trades[0].start == facts.first_start


def test_an_intraday_candidate_gets_intraday_replicates():
    cal, _, _, facts = _facts()
    assert not facts.holds_overnight
    clock = tx.TradingClock(cal)
    rng = np.random.default_rng(5)
    for _ in range(100):
        for t in tx.replicate(facts, clock, rng):
            assert clock.session_at(t.start) == clock.session_at(t.end, closing=True)


def test_an_overnight_candidate_may_hold_overnight():
    cal = _calendar(6)
    tape = _tape(cal)
    rng = np.random.default_rng(6)
    spec = _intraday(cal, rng)
    # Two long holds, each across a session boundary.
    spec += [(cal[2].end_ts - HOUR, 1), (cal[3].start_ts + 7 * HOUR, -1),
             (cal[4].end_ts - HOUR, -1), (cal[5].start_ts + 6 * HOUR, 1)]
    spec.sort()
    facts = tx.trade_facts(_blotter(spec, tape, cal), cal)
    assert facts.holds_overnight
    clock = tx.TradingClock(cal)
    crossed = sum(clock.session_at(t.start) != clock.session_at(t.end, closing=True)
                  for _ in range(100) for t in tx.replicate(facts, clock, rng))
    assert crossed > 0


# --------------------------------------------------------------------------- #
# mode 'layered': the first layer (tradesample) and the position profiles
# --------------------------------------------------------------------------- #

def _ratios(rows):
    d = lambda f: sum(p.duration for p in rows if f(p.quantity))  # noqa: E731
    return d(lambda q: q == 0), d(lambda q: q > 0), d(lambda q: q < 0)


def test_the_first_layer_matches_the_flat_total_and_the_long_short_split():
    cal = _calendar(metrics.MIN_SESSIONS + 10)
    tape = _tape(cal, 11)
    facts = tx.trade_facts(_blotter(_layered(cal, np.random.default_rng(11)),
                                    tape, cal), cal)
    flat, long_, short = _ratios(facts.periods)
    bound_long = round((facts.calendar - flat) * long_ / (long_ + short))
    rng = np.random.default_rng(12)
    for _ in range(50):
        rows = tx._first_layer(facts, rng)
        f, l_, s_ = _ratios(rows)
        assert f == flat                    # subsampled to the flat total exactly
        assert l_ < bound_long              # longs stop under their share
        assert f + l_ + s_ < facts.calendar   # shorts fill what is left, under it
        # And the share of market time long is the strategy's, give or take
        # the one row each class stops short by.
        longest = max(p.duration for p in facts.periods if p.quantity)
        assert abs(l_ - bound_long) <= longest


def test_the_first_layer_is_shuffled_rather_than_flats_then_longs_then_shorts():
    cal = _calendar(metrics.MIN_SESSIONS + 10)
    tape = _tape(cal, 13)
    facts = tx.trade_facts(_blotter(_layered(cal, np.random.default_rng(13)),
                                    tape, cal), cal)
    rows = tx._first_layer(facts, np.random.default_rng(14))
    kinds = [np.sign(p.quantity) for p in rows]
    assert kinds != sorted(kinds, key=lambda k: (k != 0, k < 0))


def test_layered_replicates_keep_each_round_turns_adds_and_max_position():
    cal = _calendar(metrics.MIN_SESSIONS + 10)
    tape = _tape(cal, 15)
    facts = tx.trade_facts(_blotter(_layered(cal, np.random.default_rng(15)),
                                    tape, cal), cal)
    observed = {p.profile for p in facts.periods if p.quantity}
    peak = max(abs(q) for prof in observed for _, q in prof)
    clock = tx.TradingClock(cal)
    rng = np.random.default_rng(16)
    seen_layered = False
    for _ in range(30):
        for t in tx.replicate(facts, clock, rng, mode="layered"):
            assert max(abs(q) for _, q in t.profile) <= peak
            assert t.profile[-1] == (t.end - t.start, 0)
            # Uncut, the profile is exactly one the strategy traded.
            if t.profile in observed:
                seen_layered |= len(t.profile) > 2
            else:
                # Cut short: a prefix of an observed profile, flattened at the cut.
                assert any(t.profile[:-1] == o[:len(t.profile) - 1] for o in observed)
    assert seen_layered


def test_flat_to_flat_keeps_only_the_opening_size():
    cal = _calendar(metrics.MIN_SESSIONS + 10)
    tape = _tape(cal, 17)
    facts = tx.trade_facts(_blotter(_layered(cal, np.random.default_rng(17)),
                                    tape, cal), cal)
    clock = tx.TradingClock(cal)
    for t in tx.replicate(facts, clock, np.random.default_rng(18), mode="flat.to.flat"):
        assert len(t.profile) == 2 and t.profile[1][1] == 0


def test_an_unknown_mode_and_layered_without_replacement_are_refused():
    cal, _, _, facts = _facts()
    clock = tx.TradingClock(cal)
    with pytest.raises(ValueError, match="mode"):
        tx.replicate(facts, clock, np.random.default_rng(0), mode="increased")
    with pytest.raises(ValueError, match="with replacement"):
        tx.replicate(facts, clock, np.random.default_rng(0), replacement=False)


# --------------------------------------------------------------------------- #
# pricing and scoring
# --------------------------------------------------------------------------- #

def test_the_then_current_price_is_the_last_trade_at_or_before():
    cal = _calendar(1)
    tape = _tape(cal)
    ts, px = tape.ts[0], tape.px[0]
    k = 100
    assert tape.at(0, int(ts[k])) == (int(ts[k]), float(px[k]))
    assert tape.at(0, int(ts[k]) + 1)[1] == float(px[k]) or ts[k + 1] == ts[k] + 1
    assert tape.at(0, int(ts[0]) - 1) == (int(ts[0]), float(px[0]))


def test_the_fast_scorer_is_the_gates_own_statistic_draw_for_draw():
    """`replicate_scores` skips the blotter. If it ever disagrees with
    fills -> reconstruct -> session_returns -> score_returns, the null is
    scoring a different number from the one the gate scores."""
    finite = 0
    cases = [(seed, overnight, layered, mode)
             for seed, overnight, layered in ((1, False, False), (2, False, True),
                                              (3, True, False), (4, True, True))
             for mode in tx.MODES]
    for seed, overnight, layered, mode in cases:
        cal = _calendar(metrics.MIN_SESSIONS + 10)
        tape = _tape(cal, seed)
        rng = np.random.default_rng(seed)
        spec = (_layered if layered else _intraday)(cal, rng)
        if overnight:
            spec = [x for x in spec if not (cal[3].start_ts <= x[0] <= cal[4].end_ts)]
            spec += [(cal[3].end_ts - HOUR, 2), (cal[4].start_ts + 6 * HOUR, 1),
                     (cal[4].start_ts + 7 * HOUR, -3)]
            spec.sort()
        facts = tx.trade_facts(_blotter(spec, tape, cal), cal)
        assert facts.layered == layered or overnight
        clock = tx.TradingClock(cal)
        for _ in range(15):
            trades = tx.replicate(facts, clock, rng, mode=mode)
            for charge in (metrics.IDLE_CHARGE, 0.0):
                fast = tx.replicate_scores(trades, clock, tape, cal,
                                           commission_per_contract=COMM,
                                           idle_charge=charge)
                slow = tx.score_replicate(trades, clock, tape, cal,
                                          commission_per_contract=COMM,
                                          idle_charge=charge)
                assert fast == pytest.approx(slow, rel=1e-12, abs=1e-12)
                finite += np.isfinite(fast)
    # Two rejection scores agree trivially; most of these must be real scores.
    assert finite > 150


def test_the_observed_order_reproduces_the_observed_score():
    """Laid out in the observed order, a candidate whose fills were at the
    then-current price IS its own replicate -- the identity draw. If this
    fails, the replicates are not built in the same units as the score."""
    cal = _calendar(metrics.MIN_SESSIONS + 10)
    tape = _tape(cal, 7)
    b = _blotter(_intraday(cal, np.random.default_rng(7)), tape, cal)
    facts = tx.trade_facts(b, cal)
    clock = tx.TradingClock(cal)

    class Identity:
        def permutation(self, n):
            return np.arange(n)

    trades = tx.replicate(facts, clock, Identity(), replacement=False,
                          mode="flat.to.flat")
    observed = score_returns(metrics.session_returns(b, cal)).primary
    assert np.isfinite(observed)
    assert tx.score_replicate(trades, clock, tape, cal,
                              commission_per_contract=COMM) == \
        pytest.approx(observed, rel=1e-12)


def test_a_layered_candidate_laid_out_in_order_is_its_own_replicate():
    """The same identity draw for `layered`: adds, partial exits and flips
    replayed from their profiles must reproduce the observed score."""
    cal = _calendar(metrics.MIN_SESSIONS + 10)
    tape = _tape(cal, 8)
    b = _blotter(_layered(cal, np.random.default_rng(8)), tape, cal)
    facts = tx.trade_facts(b, cal)
    assert facts.layered
    trades = tx.layout(facts.periods, facts, tx.TradingClock(cal), mode="layered")
    observed = score_returns(metrics.session_returns(b, cal)).primary
    assert np.isfinite(observed)
    assert tx.score_replicate(trades, tx.TradingClock(cal), tape, cal,
                              commission_per_contract=COMM) == \
        pytest.approx(observed, rel=1e-12)


def test_null_sample_is_seeded_and_refuses_a_tape_for_other_sessions():
    # More than MIN_SESSIONS, or every draw is the rejection score and two
    # seeds cannot be told apart.
    cal, tape, b, _ = _facts(n_sessions=metrics.MIN_SESSIONS + 10)
    assert all(np.isfinite(tx.null_sample(b, cal, tape,
                                          commission_per_contract=COMM, b=5)))
    one = tx.null_sample(b, cal, tape, commission_per_contract=COMM, b=30, seed=9)
    two = tx.null_sample(b, cal, tape, commission_per_contract=COMM, b=30, seed=9)
    assert one == two and len(one) == 30
    assert one != tx.null_sample(b, cal, tape, commission_per_contract=COMM,
                                 b=30, seed=10)
    with pytest.raises(ValueError, match="sessions"):
        tx.null_sample(b, cal[:-1], tape, commission_per_contract=COMM, b=2)


def test_a_candidate_with_no_round_turn_has_no_null():
    cal = _calendar(2)
    tape = _tape(cal)
    b = _blotter([(cal[0].start_ts + HOUR, 1)], tape, cal)
    assert tx.null_sample(b, cal, tape, commission_per_contract=COMM, b=5) == []
