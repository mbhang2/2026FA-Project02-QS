"""The drop-k fragility probe: how few of the best sessions kill a result.

From the Hindsight-Selection Trap (Elangovan, Zenodo 2026-08-02), which found
a signal clearing every mechanical gate while drawing 85-97% of its profit
from three trades of several hundred, independently in development and
holdout. Five of the twelve unresolved deferrals in
`scripts/open_questions.py` are the robustness seat raising that objection and
getting no resolution, because a SHARE has no bar and an integer does.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.scoring.metrics import MIN_SESSIONS, Diagnostics  # noqa: E402
from src.core.scoring.scorer import (  # noqa: E402
    DROP_PROBES,
    drop_best_fragility,
)


def _noise(n: int, mean: float, seed: int = 7) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(mean, 1.0) for _ in range(n)]


def test_a_broad_result_survives_far_more_removals_than_a_concentrated_one():
    """The property the whole probe exists for. Both series have the same
    length and both score positive; they differ only in how the profit is
    distributed, which is exactly what the robustness seat keeps objecting to
    and what `profit_concentration_top5pct` reports without a bar."""
    broad, _ = drop_best_fragility(_noise(384, 0.08))
    spiky, _ = drop_best_fragility([140.0] + _noise(383, -0.02))
    assert spiky < broad, (
        f"a result carried by one session died at k={spiky} and a broad one at "
        f"k={broad}; the probe does not discriminate")
    assert broad >= 4 * spiky, (
        f"the gap is only {broad} vs {spiky}; the probe barely separates them")


def test_the_break_point_rises_with_the_number_of_carrying_sessions():
    """Not an exact k, which depends on the noise realisation, but the
    ordering, which is what the number is read for: a result carried by more
    sessions must survive more removals. Total profit is held constant so only
    its concentration varies."""
    hits = []
    for carriers in (1, 3, 8):
        series = [140.0 / carriers] * carriers + _noise(384 - carriers, -0.02)
        hits.append(drop_best_fragility(series)[0])
    assert hits == sorted(hits) and hits[0] < hits[-1], (
        f"break points {hits} are not increasing in the number of carriers")


def test_the_curve_shows_the_decline_and_crosses_zero_at_the_break():
    hit, curve = drop_best_fragility([60.0, 55.0, 50.0] + _noise(381, -0.02))
    assert hit > 0
    vals = [curve[k] for k in sorted(curve, key=int) if curve[k] is not None]
    assert vals == sorted(vals, reverse=True), (
        f"dropping the best sessions should not raise the score: {vals}")
    below = [int(k) for k, v in curve.items() if v is not None and v <= 0]
    assert all(k >= hit for k in below), (
        f"a probe went non-positive before the reported break at {hit}")


def test_a_result_that_is_already_dead_is_reported_as_zero_not_probed():
    """0 and -1 must not be confusable: one means nothing to probe, the other
    means it survived everything that could be probed."""
    hit, curve = drop_best_fragility(_noise(384, -0.10))
    assert hit == 0
    assert curve == {}, "a dead result should not carry a curve"


def test_a_probe_is_not_run_when_it_would_cross_the_session_floor():
    """Otherwise the pre-registered floor's own rejection reads as fragility,
    which would make every short series look maximally fragile."""
    hit, curve = drop_best_fragility(_noise(MIN_SESSIONS + 1, 0.5))
    assert hit == -1, "one survivable probe should not report a break"
    assert set(curve) == {"1"}, (
        f"only k=1 fits above the floor at {MIN_SESSIONS + 1} sessions, "
        f"got probes {sorted(curve)}")


def test_the_curve_reports_every_probe_point_that_fits():
    _, curve = drop_best_fragility(_noise(384, 0.08))
    assert sorted(int(k) for k in curve) == sorted(DROP_PROBES)


def test_a_rejected_rescore_is_none_rather_than_a_number():
    """None and a negative score mean different things: one is "weak", the
    other is "the rule refused to score this at all"."""
    # every session identical once the outlier goes -> zero dispersion
    hit, curve = drop_best_fragility([99.0] + [-0.2] * 383)
    assert hit == 1
    assert curve["1"] is None


def test_order_does_not_matter_because_the_statistic_is_order_free():
    series = _noise(384, 0.08)
    a = drop_best_fragility(series)
    b = drop_best_fragility(list(reversed(series)))
    assert a == b


def test_the_fields_are_last_so_no_committed_digest_moves():
    """`Blotter.digest` covers blotter rows, but the ledger's score dicts and
    the tier-2 summary are read by things that index fields. Appending keeps
    every one of them valid."""
    names = list(Diagnostics.__dataclass_fields__)
    assert names[-2:] == ["sessions_to_zero", "primary_after_drop"]


def test_the_probe_is_published_to_the_committee():
    """A diagnostic the committee cannot see cannot resolve an objection --
    which is exactly what happened to `by_hour_utc_entry`."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import campaign_summary as cs

    for f in ("sessions_to_zero", "primary_after_drop", "by_hour_utc_entry"):
        assert f in cs.DIAGNOSTIC_FIELDS, f"{f} is computed but withheld"


def test_it_is_never_a_term_in_the_objective():
    """spx-arb-01's winner is a PROVED-real arbitrage that dies at k=9 of 105
    and loses 73% of its score on one session. A gate thresholding fragility
    would have rejected it. The probe must stay descriptive."""
    import inspect

    from src.core.scoring import scorer

    src = inspect.getsource(scorer)
    cut = src.split("def drop_best_fragility")[0]
    for name in ("sessions_to_zero", "primary_after_drop", "drop_best_fragility"):
        assert name not in cut, (
            f"{name} is referenced before the probe is defined, which means "
            f"something in the scoring path reads it")


# ------------------------------------------- the summary schema is versioned
#
# Publishing a diagnostic changes the tier-2 summary's digest, and
# test_tier_contract rebuilds committed summaries and compares. Versioning the
# field set is what lets a new diagnostic reach the committee without
# invalidating the audit chain of every summary already on disk.


def _cs():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import campaign_summary

    return campaign_summary


def test_v2_adds_fields_and_removes_none():
    cs = _cs()
    assert set(cs.V1_DIAGNOSTIC_FIELDS) < set(cs.V2_DIAGNOSTIC_FIELDS)
    assert cs.V2_DIAGNOSTIC_FIELDS[:len(cs.V1_DIAGNOSTIC_FIELDS)] == \
        cs.V1_DIAGNOSTIC_FIELDS, "v1 fields must keep their order inside v2"


def test_v1_is_frozen_at_the_set_every_committed_summary_was_digested_over():
    """If this changes, every tier2-summary/1 on disk stops verifying."""
    cs = _cs()
    assert len(cs.V1_DIAGNOSTIC_FIELDS) == 17
    for f in ("sessions_to_zero", "primary_after_drop", "by_hour_utc_entry"):
        assert f not in cs.V1_DIAGNOSTIC_FIELDS


def test_a_fresh_build_publishes_the_current_schema():
    cs = _cs()
    assert cs.SCHEMA == "tier2-campaign-summary/2"
    assert cs.DIAGNOSTIC_FIELDS is cs.SCHEMA_DIAGNOSTICS[cs.SCHEMA]


def test_an_unknown_schema_is_refused_rather_than_silently_defaulted():
    cs = _cs()
    with pytest.raises(ValueError, match="unknown summary schema"):
        cs.build(Path("/nonexistent"), schema="tier2-campaign-summary/99")


def test_a_committed_v1_summary_rebuilds_under_v1():
    """The regression this versioning exists to prevent."""
    cs = _cs()
    root = Path(__file__).resolve().parent.parent
    arm = root / "runs" / "campaigns" / "task36" / "control"
    if not (arm / "tier2-summary.json").exists():
        pytest.skip("task36/control summary not present")
    import json as _json

    stored = _json.loads((arm / "tier2-summary.json").read_text())
    assert stored["schema"] == "tier2-campaign-summary/1"
    assert cs.build(arm)["digest"] == stored["digest"]
