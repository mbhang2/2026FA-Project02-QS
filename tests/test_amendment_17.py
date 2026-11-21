"""Amendment 17: the crypto null charges each fill to its own session."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
from types import SimpleNamespace

import pytest
import yaml

from src.core.scoring.metrics import FundingEvent, SessionSpan
from src.core.scoring.randomization import round_turn as rt
from src.core.scoring.scorer import score
from src.core.types import Blotter, BlotterRow, Side

REPO = pathlib.Path(__file__).resolve().parent.parent
DAY = 86_400 * 10**9
HOUR = 3_600 * 10**9
# sha256 of the points null on `_fixture()`, computed from main at 1377a2b, before
# this amendment's code existed.
POINTS_NULL_DIGEST = "126a76af1422335dc8738c0922aca96e9b2d53600e2b8e3c5fe1b16d0faa484c"


def _cal(n: int) -> list[SessionSpan]:
    return [SessionSpan(i, i * DAY, (i + 1) * DAY - 1) for i in range(n)]


def _blotter(fills) -> Blotter:
    """fills: (ts, side, size, position_after, realized_pnl, commission)."""
    b = Blotter()
    for seq, (ts, side, size, pos, pnl, fee) in enumerate(fills, 1):
        b.add(BlotterRow(seq=seq, ts=ts, order_id=seq, side=side, price=100.0, size=size,
                         position_after=pos, commission=fee, realized_pnl=pnl))
    return b


def _fixture() -> Blotter:
    """Daily round turns, every third one held across the session boundary."""
    fills = []
    for i in range(0, 60, 2):
        close = (i + 1) * DAY + HOUR if i % 3 == 0 else i * DAY + 20 * HOUR
        fills.append((i * DAY + HOUR, Side.BUY, 1, 1, 0.0, 0.05))
        fills.append((close, Side.SELL, 1, 0, 1.0 if i % 4 else -0.7, 0.05))
    return _blotter(fills)


def _terms(n: int) -> dict:
    prices = {i: 100.0 + i for i in range(n)}
    return {"notional_base": prices,
            "idle_charge": {i: 1.2e-5 * prices[i] for i in range(n)},
            "funding": [FundingEvent(ts=i * DAY + 8 * HOUR, rate=1e-4, mark=prices[i])
                        for i in range(n)]}


def test_a_position_across_the_boundary_keeps_each_fee_in_its_session():
    b = _blotter([(23 * HOUR, Side.BUY, 1, 1, 0.0, 0.3),
                  (DAY + HOUR, Side.SELL, 1, 0, 2.0, 0.5)])
    assert rt.excursion_legs(b, _cal(2)) == [rt.ExcursionLeg(0, 0, 0.0, 0.3),
                                             rt.ExcursionLeg(0, 1, 2.0, 0.5)]


def test_a_partial_exit_in_a_later_session_shares_the_excursion_sign(monkeypatch):
    b = _blotter([(HOUR, Side.BUY, 2, 2, 0.0, 0.1), (DAY + HOUR, Side.SELL, 1, 1, 3.0, 0.1),
                  (2 * DAY + HOUR, Side.SELL, 1, 0, 5.0, 0.1)])
    legs = rt.excursion_legs(b, _cal(3))
    assert {x.excursion for x in legs} == {0}
    assert [x.session for x in legs] == [0, 1, 2]
    # Read the draws as the session vector itself. One sign for the excursion means
    # exactly two vectors; a sign per leg would give four.
    monkeypatch.setattr("src.core.scoring.scorer.score_returns",
                        lambda r: SimpleNamespace(primary=tuple(round(x, 9) for x in r)))
    draws = set(rt.null_sample(b, _cal(3), b=200, seed=1, idle_charge=0.0,
                               attribution=rt.ATTRIBUTION_FILL))
    assert draws == {(-0.1, 2.9, 4.9), (-0.1, -3.1, -5.1)}


def test_a_trailing_excursion_is_included():
    b = _blotter([(HOUR, Side.BUY, 1, 1, 0.0, 0.1), (2 * HOUR, Side.SELL, 1, 0, 1.0, 0.1),
                  (DAY + HOUR, Side.BUY, 1, 1, 0.0, 0.4)])
    assert rt.excursion_legs(b, _cal(2))[-1] == rt.ExcursionLeg(1, 1, 0.0, 0.4)
    assert len(rt.round_turns(b, _cal(2))) == 1


def test_the_no_flip_draw_is_the_score_under_fill_attribution():
    cal, terms, b = _cal(60), _terms(60), _fixture()
    observed = score(b, period=cal, **terms)
    closing = rt.identity_from_blotter(b, cal, **terms)
    fill = rt.identity_from_blotter(b, cal, attribution=rt.ATTRIBUTION_FILL, **terms)
    assert abs(closing.primary - observed.primary) > 1e-9      # the defect
    assert fill.primary == pytest.approx(observed.primary, abs=1e-9)
    rt.require_identity(observed.primary, observed.metric, fill, rt.ATTRIBUTION_FILL)


def test_a_mismatch_stops_under_fill_and_not_under_closing():
    b, cal = _fixture(), _cal(60)
    ident = rt.identity_from_blotter(b, cal, attribution=rt.ATTRIBUTION_FILL, **_terms(60))
    with pytest.raises(rt.NoFlipMismatch):
        rt.require_identity(ident.primary + 1e-6, ident.metric, ident, rt.ATTRIBUTION_FILL)
    with pytest.raises(rt.NoFlipMismatch):
        rt.require_identity(None, None, ident, rt.ATTRIBUTION_FILL)
    rt.require_identity(ident.primary + 1e-6, ident.metric, ident, rt.ATTRIBUTION_CLOSING)


def test_without_a_crossing_the_two_attributions_draw_the_same_null():
    fills = []
    for i in range(30):
        fills.append((i * DAY + HOUR, Side.BUY, 1, 1, 0.0, 0.05))
        fills.append((i * DAY + 2 * HOUR, Side.SELL, 1, 0, 1.0 if i % 3 else -1.0, 0.05))
    b, cal, terms = _blotter(fills), _cal(30), _terms(30)
    assert rt.null_sample(b, cal, b=100, seed=5, attribution=rt.ATTRIBUTION_FILL, **terms) \
        == rt.null_sample(b, cal, b=100, seed=5, **terms)


def test_the_points_null_is_bit_identical_to_before_this_amendment():
    sample = rt.null_sample(_fixture(), _cal(60), b=200, seed=11)
    assert hashlib.sha256(json.dumps(sample).encode()).hexdigest() == POINTS_NULL_DIGEST


def test_only_a_signed_amendment_on_a_notional_profile_selects_fill():
    profiles = yaml.safe_load((REPO / "config" / "run.yaml").read_text())["profiles"]
    for name, profile in profiles.items():
        assert rt.attribution_for(profile, False) == rt.ATTRIBUTION_CLOSING, name
        want = (rt.ATTRIBUTION_FILL if profile.get("scoring_units") == "notional"
                else rt.ATTRIBUTION_CLOSING)
        assert rt.attribution_for(profile, True) == want, name


def test_the_holdout_judge_applies_the_tighter_tolerance():
    spec = importlib.util.spec_from_file_location("holdout", REPO / "scripts" / "holdout.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    row = {"score": {"primary": 0.9, "metric": "sortino_per_session"}}
    facts = {"round_turns": 50, "identity_primary": 0.9 + 1e-6,
             "identity_metric": "sortino_per_session"}
    assert mod.judge(row, [-0.9] * 400, facts)["rule_ii_evaluable"]
    tight = mod.judge(row, [-0.9] * 400, {**facts, "identity_tolerance": 1e-9})
    assert not tight["rule_ii_evaluable"]
