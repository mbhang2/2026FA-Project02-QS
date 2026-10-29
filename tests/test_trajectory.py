"""The fail-closed whitelist, lifted from Gowtam's TrajectoryLeak.

What it buys over `llm._history_block`: that function selects six fields out
of a 45-key ledger row, which cannot leak but is silent when the row shape
changes -- and those rows gained diagnostic fields on 2026-10-28.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.core.research.trajectory import (  # noqa: E402
    ALLOWED, REFUSED_FOR_CAUSE, TrajectoryLeak, direction, to_trajectory,
)


def test_a_real_ledger_row_is_refused_wholesale():
    """**The whole point.** Passing a ledger row through must fail, not
    silently drop 39 fields -- including the ones the gate computes."""
    led = REPO_ROOT / "runs" / "campaigns" / "nq-mvp-01" / "ledger.jsonl"
    row = json.loads(led.read_text().splitlines()[0])
    assert len(row) > 40
    with pytest.raises(TrajectoryLeak, match="may not see"):
        to_trajectory(row)


def test_every_gate_field_is_refused_and_says_why():
    for field, reason in REFUSED_FOR_CAUSE.items():
        with pytest.raises(TrajectoryLeak) as e:
            to_trajectory({"campaign_id": "c", "outcome": "completed",
                           field: 0.01})
        assert reason in str(e.value), (
            f"{field} was refused without saying it is {reason}")


def test_a_field_nobody_has_thought_of_is_still_refused():
    """A blacklist would pass this. That is the argument for a whitelist."""
    with pytest.raises(TrajectoryLeak):
        to_trajectory({"campaign_id": "c", "outcome": "ok",
                       "some_future_diagnostic": 1})


def test_the_allowed_set_carries_no_magnitude():
    assert "score" not in ALLOWED and "primary" not in ALLOWED
    assert "score_dir" in ALLOWED
    assert not ALLOWED & set(REFUSED_FOR_CAUSE)


def test_a_clean_row_passes_and_keeps_its_fields():
    t = to_trajectory({"campaign_id": "nq-x", "outcome": "completed",
                       "trials": 400, "scored": 388, "score_dir": "up",
                       "bwis_band": "high"})
    assert (t.campaign_id, t.trials, t.score_dir, t.bwis_band) == \
        ("nq-x", 400, "up", "high")


def test_a_row_without_a_campaign_id_is_refused():
    with pytest.raises(TrajectoryLeak, match="campaign_id"):
        to_trajectory({"outcome": "completed"})


# ----------------------------------------------------------------- direction
def test_direction_reports_a_word_and_never_the_numbers():
    assert direction(0.1, 0.5) == "up"
    assert direction(0.5, 0.1) == "down"
    assert direction(0.5, 0.5) == "flat"
    assert direction(None, 0.5) is None
    assert direction(0.5, None) is None


def test_a_move_inside_the_flat_band_is_flat_not_a_direction():
    """"It moved by 1e-9" is not a direction, and a loop told it moved acts
    as though it did."""
    assert direction(1.0, 1.0 + 1e-9, flat_within=1e-6) == "flat"
    assert direction(1.0, 1.1, flat_within=1e-6) == "up"


def test_the_orchestrator_reads_trajectories_without_leaking():
    """End to end on the real repo: every campaign with a ledger goes through
    the whitelist, so the translation in `read_trajectories` is checked
    against 45 real campaigns rather than a fixture."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import orchestrate

    ts = orchestrate.read_trajectories()
    assert len(ts) > 10, f"only {len(ts)} campaigns read"
    for t in ts:
        assert t.score_dir in (None, "up", "down", "flat")
        for slot in t.__slots__:
            v = getattr(t, slot)
            assert not isinstance(v, float), (
                f"{t.campaign_id}.{slot} is a float: a magnitude escaped")
