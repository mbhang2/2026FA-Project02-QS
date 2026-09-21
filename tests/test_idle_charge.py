"""The idle-capital charge (pre-registration Amendment 3).

A session the strategy sat out is charged `IDLE_CHARGE`, pro-rated by the fraction
of the session a position was actually held. It exists because `MAR = 0` over the
full calendar made sitting out *profitable* for a losing strategy -- `sortino` goes
as `1/sqrt(n)` for fixed trades, so padding the return vector with flat sessions
drags a negative score toward zero, and an agent optimising that learns to stop
trading. `nq-mvp-01` ended on 3 trades in 384 sessions; `nq-orderflow-01`'s best
trial was 8.

These tests are about the two things that make the charge either correct or
quietly wrong: it must be pro-rated by TIME (or a one-minute round trip per
session dodges it entirely), and the identical charge vector must reach the null
(or the observed series pays a cost the null does not, and the p-value moves).
"""

from __future__ import annotations

import math
import random

import pytest

from src.core.scoring.metrics import (
    IDLE_CHARGE,
    SessionSpan,
    session_exposure,
    session_returns,
)
from src.core.scoring.scorer import score_returns
from src.core.types import Blotter, BlotterRow, Side

DAY = 6 * 3600 * 10**9          # a 6-hour session, in nanoseconds


def _calendar(n: int) -> list[SessionSpan]:
    return [SessionSpan(session=i, start_ts=i * DAY, end_ts=i * DAY + DAY - 1)
            for i in range(n)]


def _row(ts, position_after, *, pnl=0.0, comm=0.0, seq=0):
    return BlotterRow(seq=seq, ts=ts, order_id=1, side=Side.BUY, price=100.0,
                      size=1, position_after=position_after, commission=comm,
                      realized_pnl=pnl, tag="t")


def _blotter(rows) -> Blotter:
    b = Blotter()
    for r in rows:
        b.add(r)
    return b


# --------------------------------------------------------------------------- #
# exposure: the fraction of each session a position was held
# --------------------------------------------------------------------------- #

def test_a_session_with_no_fills_has_zero_exposure():
    cal = _calendar(3)
    b = _blotter([_row(0, 1), _row(DAY // 2, 0, pnl=5.0)])
    assert session_exposure(b, cal)[1] == 0.0
    assert session_exposure(b, cal)[2] == 0.0


def test_a_session_held_throughout_has_full_exposure():
    cal = _calendar(1)
    b = _blotter([_row(0, 1), _row(DAY - 1, 0, pnl=1.0)])
    assert session_exposure(b, cal)[0] == pytest.approx(1.0, abs=1e-6)


def test_half_a_session_is_half_exposed():
    cal = _calendar(1)
    b = _blotter([_row(0, 1), _row(DAY // 2, 0, pnl=1.0)])
    assert session_exposure(b, cal)[0] == pytest.approx(0.5, abs=1e-6)


def test_a_one_minute_token_trade_does_not_buy_a_whole_session():
    """The hack the 'had at least one fill' definition would let through.

    One lot held for a minute in every session dodges the charge entirely under a
    fill-count definition, while the strategy is effectively flat. Time-weighting
    charges it 99.7% of the rate. It is the same corner case one level down, and
    it is what Brian's "minimum quote size that is not zero" is guarding.
    """
    cal = _calendar(1)
    minute = 60 * 10**9
    b = _blotter([_row(0, 1), _row(minute, 0, pnl=0.0, comm=0.20)])
    frac = session_exposure(b, cal)[0]
    assert frac == pytest.approx(minute / DAY, rel=1e-3)
    assert frac < 0.01
    charged = IDLE_CHARGE * (1 - frac)
    assert charged > 0.99 * IDLE_CHARGE


def test_a_hold_spanning_a_session_boundary_is_charged_in_both():
    """Deliberately unlike P&L attribution, which books the whole result in the
    closing session because that is all a blotter can support. Exposure is not so
    limited, and pretending it is would make an overnight hold free in the session
    it was carried through."""
    cal = _calendar(2)
    b = _blotter([_row(DAY // 2, 1), _row(DAY + DAY // 2, 0, pnl=3.0)])
    ex = session_exposure(b, cal)
    assert ex[0] == pytest.approx(0.5, abs=1e-6)
    assert ex[1] == pytest.approx(0.5, abs=1e-6)


def test_a_position_still_open_at_the_end_is_not_free():
    """`_holding_periods` drops it -- its loop only appends on a return to flat.
    Reusing that directly would hand the strategy free exposure for every session
    after it stopped closing positions."""
    cal = _calendar(3)
    b = _blotter([_row(0, 1)])          # opened, never closed
    ex = session_exposure(b, cal)
    assert ex[0] == pytest.approx(1.0, abs=1e-6)
    assert ex[1] == pytest.approx(1.0, abs=1e-6)
    assert ex[2] > 0.99


# --------------------------------------------------------------------------- #
# the charge in the return vector
# --------------------------------------------------------------------------- #

def test_flat_sessions_are_charged_and_traded_ones_are_not():
    cal = _calendar(3)
    b = _blotter([_row(0, 1), _row(DAY - 1, 0, pnl=2.0, comm=0.2)])
    r = session_returns(b, cal)
    # 2.0 realised less 0.2 commission. The session was held throughout, so no
    # idle charge -- commission is a trading cost and is already in the blotter.
    assert r[0] == pytest.approx(1.8, abs=1e-6)
    assert r[1] == pytest.approx(-IDLE_CHARGE, abs=1e-9)
    assert r[2] == pytest.approx(-IDLE_CHARGE, abs=1e-9)


def test_never_trading_is_rejected_not_merely_scored_badly():
    """Every session identical at -IDLE_CHARGE has no dispersion, which the
    existing guard in `score_returns` already turns into REJECT. Stronger than the
    -1 the ratio alone would give, and it needs no new code."""
    cal = _calendar(40)
    out = score_returns(session_returns(_blotter([]), cal))
    assert out.rejected
    assert out.primary == -math.inf


def _losing_campaign(k, cal, *, spread, seed=4):
    """`k` traded sessions drawn around a negative mean, the rest flat."""
    rng = random.Random(seed)
    rows, seq = [], 0
    for s in range(k):
        rows.append(_row(s * DAY, 1, seq=seq)); seq += 1
        rows.append(_row(s * DAY + DAY - 1, 0,
                         pnl=rng.gauss(-0.5, spread), seq=seq)); seq += 1
    return score_returns(session_returns(_blotter(rows), cal)).primary


def test_the_charge_reverses_the_incentive_to_sit_out():
    """The property the whole amendment exists for: a losing strategy must not
    score better by trading less.

    Returns are dispersed because real ones are -- `nq-orderflow-01`'s trials had
    an implied downside deviation of 0.09 to 2.14 points. See the next test for
    what happens when they are not.
    """
    cal = _calendar(120)
    many, few = (_losing_campaign(100, cal, spread=1.5),
                 _losing_campaign(10, cal, spread=1.5))
    assert many > few, (
        f"trading 100 sessions scored {many:+.4f} and trading 10 scored {few:+.4f}; "
        "the charge has not reversed the sit-out incentive")


def test_the_charge_does_NOT_reverse_it_for_a_low_dispersion_heavy_loser():
    """A known bound, asserted so it is a documented limit and not a surprise.

    The charge competes with the downside deviation in the denominator. A
    strategy losing far more per session than the charge, with little dispersion,
    still scores better by trading less: the charge cannot outweigh a per-session
    loss several times its size.

    It is a narrow case and a benign one -- such a strategy scores near -1 and
    sits at the bottom of the campaign either way, so the gate is unaffected and
    only the ordering *among hopeless candidates* is wrong. But it means the
    charge is a fix for the regime the agent actually converges in, not a
    universal one, and the pre-registration says so.
    """
    cal = _calendar(120)
    many, few = (_losing_campaign(100, cal, spread=0.0),
                 _losing_campaign(10, cal, spread=0.0))
    assert many < few, (
        "this bound no longer holds -- if the charge now reverses the incentive "
        "even for a zero-dispersion heavy loser, the limitation recorded in "
        "Amendment 3 is out of date and should be removed rather than left")
    assert many < -0.9 and few < -0.9, (
        "both should still be terrible scores; the bound is about ordering among "
        "hopeless candidates, not about either of them looking acceptable")


# --------------------------------------------------------------------------- #
# the null must see the identical charge
# --------------------------------------------------------------------------- #

def test_the_null_carries_the_same_charge_as_the_observed_series():
    """The failure this prevents is not hypothetical: a charge on the observed
    series that the null does not pay moved a measured p from 0.0367 to 0.1813,
    because the null is handed free upside on sessions that never had a bet."""
    from src.core.scoring.randomization.round_turn import null_sample, round_turns

    cal = _calendar(60)
    rows, seq = [], 0
    for s in range(0, 60, 2):                     # trades every other session
        rows.append(_row(s * DAY, 1, seq=seq)); seq += 1
        rows.append(_row(s * DAY + DAY // 2, 0, pnl=(1.0 if s % 4 else -1.0),
                         comm=0.2, seq=seq)); seq += 1
    b = _blotter(rows)
    draws = null_sample(b, cal, b=200, seed=3)
    assert draws, "the null produced no draws"
    # Every draw pays the same idle cost, so none can beat a strategy that simply
    # avoided it -- the null's centre must sit below the no-charge case.
    assert all(math.isfinite(d) for d in draws)
    charge_total = sum(IDLE_CHARGE * (1 - f) for f in session_exposure(b, cal))
    assert charge_total > 0, "this fixture should have idle sessions"


def test_the_blotter_is_untouched_by_the_charge():
    """The charge lives in the return vector, not as synthetic fills. If it were
    fills, `trade_count`, commission, gross P&L, the round-turn construction and
    the reconciliation digest would all need to know about it, and each is a place
    to forget."""
    cal = _calendar(10)
    rows = [_row(0, 1), _row(DAY - 1, 0, pnl=2.0, comm=0.2)]
    b = _blotter(rows)
    assert len(b.rows) == 2
    assert b.commission == pytest.approx(0.2)
    assert b.realized_pnl == pytest.approx(2.0)
    session_returns(b, cal)
    assert len(b.rows) == 2, "the charge added rows to the blotter"
    assert b.commission == pytest.approx(0.2)
    assert b.realized_pnl == pytest.approx(2.0)


def test_the_null_draws_actually_move_when_the_charge_is_applied():
    """Stronger than checking the call succeeds: the draws must CHANGE.

    If `null_sample` ignored the charge the observed series would pay a cost the
    null does not, which is the failure that moves a measured p from 0.0367 to
    0.1813. A test that only asserts the null runs would pass throughout that bug.
    """
    from src.core.scoring.randomization.round_turn import null_sample

    cal = _calendar(60)
    rows, seq = [], 0
    for s in range(0, 60, 2):
        rows.append(_row(s * DAY, 1, seq=seq)); seq += 1
        rows.append(_row(s * DAY + DAY // 2, 0,
                         pnl=(1.0 if s % 4 else -1.0), comm=0.2, seq=seq)); seq += 1
    b = _blotter(rows)
    charged = null_sample(b, cal, b=120, seed=3)
    free = null_sample(b, cal, b=120, seed=3, idle_charge=0.0)
    assert len(charged) == len(free) == 120
    assert charged != free, "the idle charge never reached the null draws"
    # Every draw pays the same idle cost, so the charged null sits lower.
    assert sum(charged) / len(charged) < sum(free) / len(free)


def test_a_wrong_length_charge_vector_is_refused():
    """The charge and the session axis must agree. Silently broadcasting or
    truncating would make the null score a different calendar from the observed
    series, which is the same like-for-like failure by another route."""
    from src.core.scoring.randomization.round_turn import RoundTurn, null_from_turns

    turns = [RoundTurn(gross=1.0, cost=0.2, session=0, n_fills=2),
             RoundTurn(gross=-1.0, cost=0.2, session=2, n_fills=2)]
    with pytest.raises(ValueError, match="charges has 2 entries for 4 sessions"):
        null_from_turns(turns, [0, 1, 2, 3], b=5, charges=[0.0, 0.0])


def test_the_null_stays_calibrated_under_the_charge():
    """A false-positive rate near nominal on no-edge data, with the charge on.

    Small R and B keep this inside a unit-test budget, so it is a tripwire rather
    than the measurement -- the measurement is Task 57's, at C = 400 and
    B = 10,000. What it catches is the charge breaking calibration outright.
    """
    from src.core.scoring.randomization.round_turn import null_sample

    cal = _calendar(60)
    rejects = 0
    R, B = 40, 150
    for trial in range(R):
        rng = random.Random(900 + trial)
        rows, seq = [], 0
        for s in range(0, 60, 2):                      # no edge: symmetric draws
            rows.append(_row(s * DAY, 1, seq=seq)); seq += 1
            rows.append(_row(s * DAY + DAY // 2, 0, pnl=rng.gauss(0.0, 1.0),
                             comm=0.2, seq=seq)); seq += 1
        blot = _blotter(rows)
        obs = score_returns(session_returns(blot, cal)).primary
        draws = null_sample(blot, cal, b=B, seed=900 + trial)
        p = (1 + sum(d >= obs for d in draws)) / (len(draws) + 1)
        rejects += p <= 0.05
    rate = rejects / R
    assert rate <= 0.25, (
        f"false-positive rate {rate:.2f} at nominal 0.05 over R={R}; the charge "
        "appears to have broken the null's calibration")


# --------------------------------------------------------------------------- #
# the config must reach the code -- the Task 60 lesson, applied in advance
# --------------------------------------------------------------------------- #

def test_run_py_reads_the_configured_idle_charge():
    """Task 60's bug was a value declared in run.yaml that never reached the
    engine: eleven profile keys were consumed and `commission_per_contract` was
    not one of them, so nq-mvp-01 ran at a tenth of its stated cost while every
    artefact said otherwise. A config knob nothing reads is worse than no knob,
    because the record claims it was applied.
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "run.py").read_text()
    body = src[src.index("def _run_and_score"):src.index("def _failure_record")]
    assert "idle_charge" in body, "run.py scores without reading the idle charge"
    assert 'cfg.get("campaign", {}).get("idle_charge"' in body, (
        "the idle charge is not being read from the campaign config")
    # Scoring and diagnostics must be handed the SAME value, or the recorded
    # diagnostics describe a return vector the score was not computed from.
    assert body.count("idle_charge=idle_charge") == 2, (
        "score() and score_diagnostics() must both receive the configured charge")


def test_the_config_declares_an_idle_charge():
    from pathlib import Path

    import yaml

    cfg = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config/run.yaml").read_text())
    assert "idle_charge" in cfg["campaign"], (
        "config/run.yaml no longer declares campaign.idle_charge; the scored "
        "metric would silently fall back to the module default")
    assert cfg["campaign"]["idle_charge"] == pytest.approx(IDLE_CHARGE), (
        "the configured charge and the module default disagree. One of them is "
        "what actually runs and the other is what a reader believes.")
