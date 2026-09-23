"""Whether the proposing agent may write a strategy that fits.

A yes/no the campaign registers before trial 0. It can be that simple because the
architecture already forecloses the dangerous half: `fit_sessions` lives in a
profile, profiles live in `config/run.yaml`, and `check_agent_paths.py` lets the
agent write only `src/strategies/`. So the agent cannot move the fit window --
the one knob where a bad choice yields a *flattering* score rather than a bad one.
What is left is whether it may fit at all.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.loop.campaign import (  # noqa: E402
    CampaignRecord, declares_fit, run_trial,
)

FITTED = """
class Strategy:
    def setup(self, ctx): ctx.subscribe('5m')
    def on_bars(self, ctx): pass
    def on_fill(self, ctx): pass
    def on_lifecycle(self, ctx): pass
    def fit(self): self.threshold = 1.0
    def save_weights(self): return b"1.0"
    def load_weights(self, blob): self.threshold = float(blob)
"""

PLAIN = """
class Strategy:
    def setup(self, ctx): ctx.subscribe('5m')
    def on_bars(self, ctx): pass
    def on_fill(self, ctx): pass
    def on_lifecycle(self, ctx): pass
"""


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #
def test_a_fitted_proposal_is_recognised():
    assert declares_fit(FITTED) is True
    assert declares_fit(PLAIN) is False


def test_detection_is_by_ast_not_by_substring():
    """`"fit" in source` matches a comment, a docstring, and the word profit."""
    for innocent in (
        '# this should fit the data well\nclass Strategy:\n    pass\n',
        'class Strategy:\n    """maximise profit"""\n    pass\n',
        'class Strategy:\n    benefit = 1\n',
        'class Strategy:\n    def profit(self): pass\n',
    ):
        assert declares_fit(innocent) is False, innocent

    assert declares_fit("class S:\n    def fit(self): pass\n") is True


def test_broken_source_is_not_a_fitted_strategy():
    """It is a broken one, and run.py reports that better than this could."""
    assert declares_fit("class Strategy:\n    def fit(self)\n") is False


# --------------------------------------------------------------------------- #
# the refusal
# --------------------------------------------------------------------------- #
def test_a_fitted_proposal_is_refused_when_the_campaign_did_not_register_it(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("")
    before = (REPO_ROOT / "src" / "strategies" / "current.py").read_text()

    row = run_trial(FITTED, profile="harness", ledger=ledger,
                    campaign_id="c", index=0, allow_fitted=False)

    assert row["outcome"] == "refused_fitted"
    assert row["score"] is None
    assert "did not register" in row["error"]
    # Refused before anything was touched: the agent's file is untouched and the
    # proposal never reached the data.
    assert (REPO_ROOT / "src" / "strategies" / "current.py").read_text() == before
    assert ledger.read_text() == "", "a refused proposal must not write a row"


def test_none_means_no(tmp_path):
    """The default. A campaign that never considered the question has not
    permitted it."""
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("")
    row = run_trial(FITTED, profile="harness", ledger=ledger,
                    campaign_id="c", index=0, allow_fitted=None)
    assert row["outcome"] == "refused_fitted"


def test_a_plain_proposal_is_never_refused_by_this_gate(tmp_path):
    """The gate must be invisible to every campaign that is not about fitting."""
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("")
    row = run_trial(PLAIN, profile="harness", ledger=ledger,
                    campaign_id="c", index=0, allow_fitted=False)
    assert row["outcome"] != "refused_fitted"


# --------------------------------------------------------------------------- #
# the record
# --------------------------------------------------------------------------- #
def test_adding_the_field_did_not_move_any_existing_records_digest():
    """**The reason the default is `None` and not `False`.**

    `digest()` drops None keys. A `False` default would be serialised, every
    campaign record committed before this field existed would hash differently,
    and `CampaignRecord.read` would raise `CampaignTampered` on records nobody
    touched -- a tamper alarm crying wolf, which is worse than no alarm.
    """
    base = CampaignRecord(campaign_id="x", budget=50, profile="harness", q=0.05)
    assert base.allow_fitted_proposals is None
    assert "allow_fitted_proposals" not in json.loads(
        json.dumps({k: v for k, v in base.__dict__.items() if v is not None},
                   default=str))


def test_every_committed_campaign_record_still_reads():
    """The check above, against the real files rather than a constructed one."""
    found = list((REPO_ROOT / "runs" / "campaigns").glob("*/campaign.json"))
    assert found, "no campaign records on disk; this check has gone blind"
    for path in found:
        CampaignRecord.read(path)      # raises CampaignTampered if the digest moved


def test_the_flag_is_registered_in_the_record_not_read_at_trial_time(tmp_path):
    """Frozen before trial 0. Turning it on partway through because the unfitted
    trials looked poor is a different experiment from the registered one."""
    rec = CampaignRecord(campaign_id="x", budget=3, profile="harness", q=0.05,
                          allow_fitted_proposals=True)
    path = tmp_path / "campaign.json"
    digest = rec.write(path)
    back, stored = CampaignRecord.read(path)
    assert back.allow_fitted_proposals is True and stored == digest

    # and flipping it after the fact is caught
    body = json.loads(path.read_text())
    body["record"]["allow_fitted_proposals"] = False
    path.write_text(json.dumps(body))
    try:
        CampaignRecord.read(path)
    except Exception as exc:
        assert "does not hash to what it says" in str(exc)
    else:
        raise AssertionError("an edited flag was not caught")


def test_the_flag_and_the_profile_are_two_gates_in_series(tmp_path):
    """Permission is not a fit window.

    `allow_fitted_proposals: true` lets a fitted proposal past *this* gate. It
    still has to meet a profile that declares `fit_sessions`, or
    `RemoteStrategy._check_capabilities` refuses the run -- because a class with
    `fit()` under a profile with no fit window would never be fitted at all and
    would score untrained.

    So the human controls both halves and they are separate: whether the agent
    may fit, and what it is allowed to fit on.
    """
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("")
    before = (REPO_ROOT / "src" / "strategies" / "current.py").read_text()

    row = run_trial(FITTED, profile="harness", ledger=ledger,
                    campaign_id="c", index=0, allow_fitted=True)

    assert (REPO_ROOT / "src" / "strategies" / "current.py").read_text() == before, (
        "current.py was not restored after the trial")
    # Past the proposal gate...
    assert row["outcome"] != "refused_fitted"
    # ...and stopped by the profile, which declares no fit window.
    assert row.get("outcome") != "completed", (
        "a fitted strategy ran under a profile with no fit_sessions; fit() would "
        "never have been called and the model would be scored untrained")
