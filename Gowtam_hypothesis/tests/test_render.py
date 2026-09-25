import pathlib
import sys

import pytest

from researcher.render import render
from researcher.schemas import DataRequires, HypothesisRecord, SourceClaim

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _record() -> HypothesisRecord:
    return HypothesisRecord(
        id="intraday-continuation-01",
        mechanism="An outsized move inside a session reflects a temporary "
                  "imbalance more often than new information.",
        falsifiable="If the effect is real, the price drifts back more often "
                     "than it continues after the largest moves.",
        operationalize="Measure the session move against its own realised "
                       "range so far; fade it after a threshold in units of "
                       "that range.",
        data_requires=DataRequires(interval="5m"),
        sources=(SourceClaim(claim="the effect is real in equity index "
                                   "intraday bars",
                             source_url="https://arxiv.org/abs/0000.00000",
                             source_title="Example preprint"),),
        confidence_prior="Widely claimed, cheap to test.",
    )


def test_render_contains_expected_sections():
    text = render(_record())
    for heading in ("## The mechanism, and why it might work",
                    "## The claim this makes testable",
                    "## How to make it concrete",
                    "## Data it needs",
                    "## Where it came from"):
        assert heading in text, f"missing {heading!r}"
    assert "arxiv.org" in text


def test_render_is_deterministic():
    assert render(_record()) == render(_record())


def test_render_passes_main_repo_lint():
    # The rail's lint is the final authority; the renderer's own boilerplate must
    # be clean of gate vocabulary. Import from the main repo (add its root) and
    # assert a representative render passes. Skips when the MVP checkout is not
    # importable, so this test still runs in this workstream alone.
    sys.path.insert(0, str(_REPO_ROOT))
    hypothesis = pytest.importorskip("src.core.loop.hypothesis")
    text = render(_record())
    try:
        hypothesis.lint(text, source="test")
    except hypothesis.HypothesisRejected as exc:
        pytest.fail(f"renderer emitted gate vocabulary:\n{exc}")
