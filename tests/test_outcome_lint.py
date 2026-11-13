"""The realised-outcome lint: catches a stated market path, passes a method."""
from __future__ import annotations

import pathlib

import pytest

from src.core.research.outcome_lint import findings

ROOT = pathlib.Path(__file__).resolve().parent.parent

LEAKS = [
    "NQ fell 4% in March after the FOMC.",
    "In 2024 the Nasdaq rallied hard into the close.",
    "ES closed up 1.2% on the session.",
    "NQ's current price is near 21,500.",
    "The current price is 21500 so size accordingly.",
    "On 2025-03-14 the index dropped 300 points.",
    "QQQ surged after earnings.",
    "It rose by about 3 percent in June 2025.",
    "Since the March 2025 selloff, NQ has recovered.",
    "The market dropped 300 points that afternoon.",
]

METHODS = [
    "Enter when price hits the opening range high.",
    "Use the average range over the last 20 sessions.",
    "NQ's minimum tick is 0.25 points.",
    "NQ is $20 a point, so a round trip costs about $4.00.",
    "Written 2026-09-21, before the campaign.",
    "Exit if the trade has lost 8 points from entry.",
    "A session that closed up is followed by a gap more often than one that "
    "closed down.",
    "The stop is 12 ticks.",
]


@pytest.mark.parametrize("text", LEAKS)
def test_a_stated_market_path_is_caught(text):
    assert findings(text), f"missed a realised outcome: {text!r}"


@pytest.mark.parametrize("text", METHODS)
def test_method_language_is_not_a_leak(text):
    """Each of these was a false positive of the version this was ported from,
    or is how a strategy ordinarily describes itself."""
    assert not findings(text), findings(text)


# Amendment 11 refinements that quote an earlier campaign's profit by month.
# A refinement may read train results, so they are legitimate THERE; the lint
# runs only on questions written from a researcher's words. Named rather than
# globbed away, so a new file that trips it is looked at.
REFINEMENTS_QUOTING_TRAIN_RESULTS = {
    "opening-range-exit-hold-or-return.md",
    "outsized-move-continuation-refined.md",
}


def test_every_other_hypothesis_on_record_passes():
    """The 19-of-25 false-positive rate of the original is the thing this
    port exists to fix, so the files on record are its regression suite."""
    files = sorted((ROOT / "config" / "hypotheses").glob("*.md"))
    if not files:
        pytest.skip("no hypothesis files in this checkout")
    hit = {f.name: findings(f.read_text()) for f in files
           if f.name not in REFINEMENTS_QUOTING_TRAIN_RESULTS}
    assert not {k: v for k, v in hit.items() if v}


def test_one_finding_names_the_text_to_change():
    out = findings("NQ fell 4% in March.")
    assert out and '"NQ fell' in out[0]
