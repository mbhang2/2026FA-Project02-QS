"""`scripts/campaign.py` renders its own summary.

**Why this file exists.** `render()` read `summary["correction"]` at the top
level. Task 32 demoted BH and Holm to diagnostics and moved that key under
`diagnostics_only`, putting the real decision under `gate`; `render()` was
never updated. It has no test, and the stub campaigns on disk show it was last
run on 2026-09-19 -- so it crashed with `KeyError: 'correction'` for forty days
and nobody saw it, because the only way to see it was to run it.

It surfaced when three stub campaigns were run to check the merged branch. The
trials all completed; only the reporting died, which is the shape of defect a
test catches and a glance does not.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from campaign import render  # noqa: E402

from src.core.loop.campaign import summarise  # noqa: E402


def _minimal(**over) -> dict:
    """A summary of the shape `summarise()` produces today."""
    body = {
        "campaign_id": "t", "record_digest": "0" * 32, "record_tampered": False,
        "budget": 10, "q": 0.05, "n_trials": 10, "harness_failures": 0,
        "unaccounted": 0, "outcomes": {"completed": 10},
        "per_trial_accepts_at_0.001": 1, "wall_clock_s": 1.0,
        "diagnostics_only": {
            "note": "diagnostics",
            "correction": {"bh_discoveries": 2, "holm_discoveries": 1,
                           "uncorrected_discoveries": 7, "decisions": []},
        },
        "gate": {"is_the_gate": False, "note": "no per-candidate nulls",
                 "resolution": {"r": 2000}},
    }
    body.update(over)
    return body


def test_render_does_not_crash_on_the_shape_summarise_produces():
    """The regression. A KeyError here is the bug this file was written for."""
    out = render(_minimal())
    assert "campaign" in out and "THE GATE" in out


def test_the_gate_is_rendered_before_the_diagnostics():
    """A reader must not mistake a BH discovery count for an accept. The old
    output put a section headed CORRECTED where the decision should be."""
    out = render(_minimal())
    assert out.index("THE GATE") < out.index("DIAGNOSTICS ONLY")
    assert "not the gate" in out


def test_no_per_candidate_nulls_renders_as_no_decision_not_as_a_reject():
    out = render(_minimal())
    assert "no accept decision" in out
    assert "ACCEPT" not in out, (
        "a campaign with no nulls must not print an ACCEPT line at all")


def test_a_real_gate_renders_its_accept():
    out = render(_minimal(gate={
        "is_the_gate": True, "p": 0.0026, "reject": True,
        "observed_best_positive": True, "accept": True}))
    assert "ACCEPT" in out and "0.0026" in out


def test_bh_and_holm_are_labelled_diagnostics_wherever_they_appear():
    out = render(_minimal())
    head = out[:out.index("Benjamini")]
    assert "DIAGNOSTICS ONLY" in head, (
        "BH appears before any line saying it is a diagnostic")


def test_a_summary_missing_the_diagnostics_block_still_renders():
    """Defensive: the crash was a missing key, so absence must not re-break it."""
    body = _minimal()
    del body["diagnostics_only"]
    out = render(body)
    assert "THE GATE" in out


def test_the_keys_render_reads_are_the_keys_summarise_writes():
    """The actual contract. If `summarise` renames a key again, this fails
    here rather than forty days later in someone's terminal."""
    import inspect

    src = inspect.getsource(summarise)
    for key in ('"gate"', '"diagnostics_only"', '"uncorrected"'):
        assert key in src, f"summarise no longer writes {key}; render reads it"


def test_it_renders_a_committed_stub_summary_if_one_is_present():
    for name in ("task53-stub-linux-pinned", "task53-stub-linux", "task52-stub"):
        p = REPO_ROOT / "runs" / "campaigns" / name / "summary.json"
        if p.is_file():
            render(json.loads(p.read_text()))
            return
    pytest.skip("no committed stub summary to render")
