"""The loop: trial driver, campaign runner, and the correction over the ledger.

The correction is the project's stated contribution and nothing consumed the FAR
column until Task 30. These tests pin the arithmetic against a published worked
example, pin the accounting rules that a campaign could most easily cheat, and
pin the pre-registration guard.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.core.loop.agents import NullGenerator, ReplayList  # noqa: E402
from src.core.loop.campaign import (  # noqa: E402
    TRIAL_OUTCOMES, CampaignRecord, CampaignResult, CampaignTampered,
    new_campaign_id, run_campaign, summarise,
)
from src.core.scoring.correction import correct, deflation_view  # noqa: E402

# Benjamini & Hochberg (1995), Table 1. The published answers are 4 BH
# discoveries and 3 Holm at q=0.05 -- a check against arithmetic somebody else
# did, rather than against my own reimplementation of it.
BH95 = [0.0001, 0.0004, 0.0019, 0.0095, 0.0201, 0.0278, 0.0298, 0.0344,
        0.0459, 0.3240, 0.4262, 0.5719, 0.6528, 0.7590, 1.000]


# --------------------------------------------------------------------------- #
# the correction
# --------------------------------------------------------------------------- #
def test_the_correction_reproduces_the_published_worked_example():
    r = correct(BH95, q=0.05)
    assert r.bh_discoveries == 4, "Benjamini & Hochberg (1995) Table 1 says 4"
    assert r.holm_discoveries == 3, "the same table says 3 for Holm"
    assert r.n == len(BH95)


def test_holm_is_never_more_generous_than_bh():
    """FWER control is strictly stronger than FDR control, so Holm cannot accept
    something BH rejects. A swept property rather than one example, because this
    is the kind of thing an off-by-one in a step procedure breaks in one
    direction only."""
    import random

    rng = random.Random(11)
    for _ in range(200):
        fars = [rng.random() ** 3 for _ in range(rng.randint(1, 60))]
        r = correct(fars, q=rng.choice((0.01, 0.05, 0.1)))
        assert r.holm_discoveries <= r.bh_discoveries
        assert r.bh_discoveries <= r.uncorrected_discoveries


def test_the_step_up_can_accept_a_p_value_above_its_own_critical_value():
    """The property that distinguishes BH from a naive per-rank comparison: a
    trial is carried by smaller p-values further down the ordering. Worth pinning
    because 'accept where p <= critical' is the obvious wrong implementation."""
    # rank 4 of 5 has critical 4/5 x 0.05 = 0.04, and its p is 0.045 -- above
    # its own bar. It is accepted anyway because rank 5's p of 0.05 clears
    # 5/5 x 0.05, which carries everything below it.
    r = correct([0.001, 0.002, 0.003, 0.045, 0.05], q=0.05)
    assert r.bh_discoveries == 5
    carried = [d for d in r.decisions if d.rank == 4][0]
    assert carried.far > carried.bh_critical and carried.bh_accepted


def test_nothing_is_filtered_out_of_n():
    """A crashed trial enters at the worst possible p-value rather than being
    dropped. Dropping it would shrink the denominator by exactly the trials that
    did not work, which is the most natural way to cheat this correction."""
    r = correct([0.0001] + [1.0] * 99, q=0.05)
    assert r.n == 100
    assert r.bh_discoveries == 1
    # and the same p-value in a campaign of 10 is a much easier bar
    assert correct([0.0001] + [1.0] * 9, q=0.05).bh_discoveries == 1
    assert r.decisions[0].bh_critical < correct(
        [0.0001] + [1.0] * 9, q=0.05).decisions[0].bh_critical


def test_the_deflation_view_reports_a_margin_and_says_it_is_not_a_rate():
    d = deflation_view(1.0, 200, 100, dataset="synthetic")
    assert d["expected_max_under_null"] > 0 or d["expected_max_under_null"] < 0
    assert d["margin"] == pytest.approx(1.0 - d["expected_max_under_null"])
    assert "not a p-value" in d["note"]


# --------------------------------------------------------------------------- #
# pre-registration
# --------------------------------------------------------------------------- #
def _record(tmp_path, **kw) -> CampaignRecord:
    base = dict(campaign_id="test", profile="campaign", budget=3, q=0.05,
                agent="stub", agent_seed=0, dataset="synthetic", split="train")
    base.update(kw)
    return CampaignRecord(**base)


def test_the_campaign_record_is_hashed_and_a_later_edit_is_caught(tmp_path):
    """A budget or a q chosen after seeing results is the failure this whole
    project exists to prevent. Building the loop without this guard would have
    been an embarrassing place to commit it."""
    path = tmp_path / "campaign.json"
    record = _record(tmp_path, budget=200, q=0.05)
    digest = record.write(path)

    back, stored = CampaignRecord.read(path)
    assert stored == digest and back.budget == 200

    for field, value in (("budget", 20), ("q", 0.2), ("stopping_rule", "when good")):
        body = json.loads(path.read_text())
        body["record"][field] = value
        path.write_text(json.dumps(body))
        with pytest.raises(CampaignTampered, match="does not hash"):
            CampaignRecord.read(path)


def test_the_record_carries_what_was_pre_registered(tmp_path):
    record = _record(tmp_path)
    assert record.stopping_rule.startswith("fixed budget")
    assert "benjamini_hochberg" in record.corrections
    record.write(tmp_path / "c.json")
    written = json.loads((tmp_path / "c.json").read_text())["record"]
    for key in ("budget", "q", "stopping_rule", "corrections", "agent_seed",
                "dataset", "split"):
        assert key in written


# --------------------------------------------------------------------------- #
# accounting
# --------------------------------------------------------------------------- #
def test_a_crashed_trial_counts_toward_n(tmp_path):
    """Verified end to end with a generator that crashes on a known fraction,
    rather than asserted."""
    ledger = tmp_path / "ledger.jsonl"
    record = _record(tmp_path, campaign_id=new_campaign_id(), budget=6)
    agent = NullGenerator(seed=3, crash_fraction=1.0)   # every proposal raises
    result = run_campaign(agent, record, ledger=ledger,
                          record_path=tmp_path / "campaign.json", progress=False)

    assert len(result.rows) == 6
    assert all(r["outcome"] == "crashed" for r in result.trials)
    assert len(result.trials) == 6, "a crashed trial is a trial"
    summary = summarise(result, dataset="synthetic")
    assert summary["n_trials"] == 6
    assert summary["correction"]["n"] == 6
    assert summary["correction"]["bh_discoveries"] == 0
    assert summary["outcomes"] == {"crashed": 6}


def test_harness_failures_are_excluded_from_n_and_visible():
    """The one exclusion, and it is not a trial: the engine, the pipe or the
    platform failed rather than the candidate. A harness bug counted as a crashed
    trial inflates N and quietly changes every corrected p-value."""
    assert "harness_failed" not in TRIAL_OUTCOMES
    for outcome in ("completed", "crashed", "abandoned_on_budget"):
        assert outcome in TRIAL_OUTCOMES

    result = CampaignResult(
        record=CampaignRecord(campaign_id="x", profile="campaign", budget=3,
                              q=0.05),
        record_digest="d",
        rows=[{"outcome": "completed", "score": None, "campaign_index": 0},
              {"outcome": "crashed", "score": None, "campaign_index": 1},
              {"outcome": "harness_failed", "score": None, "campaign_index": 2}],
    )
    assert len(result.trials) == 2
    assert len(result.harness_failures) == 1
    summary = summarise(result, dataset="synthetic")
    assert summary["n_trials"] == 2 and summary["harness_failures"] == 1
    assert summary["outcomes"]["harness_failed"] == 1


def test_a_campaign_runs_on_the_training_split_and_nowhere_else(tmp_path):
    import yaml

    cfg = yaml.safe_load((REPO_ROOT / "config" / "run.yaml").read_text())
    assert cfg["profiles"]["campaign"]["split"] == "train", (
        "a campaign that searched on validation would consume the split it is "
        "eventually judged against"
    )

    ledger = tmp_path / "ledger.jsonl"
    record = _record(tmp_path, campaign_id=new_campaign_id(), budget=2)
    result = run_campaign(NullGenerator(seed=7), record, ledger=ledger,
                          record_path=tmp_path / "campaign.json", progress=False)
    assert {r["split"] for r in result.trials} == {"train"}


def test_every_row_carries_the_campaign_id(tmp_path):
    record = _record(tmp_path, campaign_id="abc123", budget=2)
    result = run_campaign(ReplayList([_INERT, _INERT]), record,
                          ledger=tmp_path / "l.jsonl",
                          record_path=tmp_path / "c.json", progress=False)
    assert [r["campaign_id"] for r in result.rows] == ["abc123", "abc123"]
    assert [r["campaign_index"] for r in result.rows] == [0, 1]


def test_the_stub_ignores_history_and_says_so():
    """What a real agent sees is a Task 31 decision. Baking an answer in here
    would settle it by accident."""
    import inspect

    from src.core.loop import agents

    assert "Task 31" in agents.__doc__
    for cls in (NullGenerator, ReplayList):
        src = inspect.getsource(cls.propose)
        assert "del history" in src


_INERT = '''
class Strategy:
    def setup(self, ctx): ctx.subscribe("5m")
    def on_bars(self, ctx): pass
    def on_fill(self, ctx): pass
    def on_lifecycle(self, ctx): pass
'''
