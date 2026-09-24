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


# --------------------------------------------------------------------------- #
# the campaign driver: does any of this reach a real campaign?
# --------------------------------------------------------------------------- #
def _nq_campaign():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "nq_campaign", REPO_ROOT / "scripts" / "nq_campaign.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_agent_is_told_when_it_is_in_a_fitted_campaign():
    """**The failure this prevents is silent in both directions.**

    An agent told the split's full length is told the wrong session count, and an
    agent never told a fit window exists will not use one -- `strategy-api.md`
    7c says a `fit()` proposal is refused unless the campaign registered it. So
    a fitted campaign whose `run_facts` does not say so gets ordinary strategies
    and a wrong session count, and nothing raises.
    """
    import yaml

    m = _nq_campaign()
    profiles = yaml.safe_load((REPO_ROOT / "config" / "run.yaml").read_text())["profiles"]

    plain = m.run_facts(profiles["nq-train"], 384)
    assert "384 scored sessions" in plain
    assert "fitted campaign" not in plain.lower(), (
        "an unfitted campaign's agent is being told about a fit window")

    fitted = m.run_facts(profiles["nq-train-fitted"], 384)
    assert "This is a fitted campaign" in fitted
    assert "284 sessions" in fitted, "the fit window's size is not stated"
    assert "100 scored sessions" in fitted, (
        "the agent is told the split's length rather than what it is scored on")
    assert "fit()" in fitted and "save_weights" in fitted


def test_the_scored_count_is_derived_and_not_hardcoded():
    """384 is the train split's length. The scored count is that minus the fit
    window, and a fitted campaign that reported 384 would be describing a run
    nobody is having."""
    m = _nq_campaign()
    made_up = {"split": "train", "fill_model": "unfavorable",
               "commission_per_contract": 0.10, "fit_sessions": 300}
    facts = m.run_facts(made_up, 384)
    assert "84 scored sessions" in facts and "300 sessions" in facts


def test_the_registration_freezes_the_flag_and_the_profile():
    """Both travel in `campaign.json`, so later stages read the decision rather
    than re-deciding it from a module constant."""
    source = (REPO_ROOT / "scripts" / "nq_campaign.py").read_text()
    assert 'allow_fitted_proposals=bool(cc.get("allow_fitted_proposals"))' in source, (
        "the campaign record does not freeze the proposal flag")
    assert "profile=profile_name" in source, (
        "the record hardcodes PROFILE, so a fitted campaign cannot be registered")


def test_the_run_stage_passes_the_flag_and_the_records_profile():
    """The two places the wiring would go dead without anything raising."""
    source = (REPO_ROOT / "scripts" / "nq_campaign.py").read_text()
    assert "allow_fitted=record.allow_fitted_proposals" in source, (
        "run_trial is called without the gate, so a fitted proposal would run "
        "in a campaign that did not register one")
    assert "record.profile, model)" in source, (
        "the agent is built from PROFILE rather than from the record, so a "
        "fitted campaign's agent would be given the unfitted run facts")


def test_permission_without_a_fit_window_is_recorded_as_no():
    """**Permission and capability are different things.**

    The config says whether the agent may fit; the profile says whether there is
    a window to fit in. A fitted proposal in a campaign with `fit_sessions: 0`
    passes the proposal gate and is then refused by the engine for having no
    window -- a wasted trial rather than a useful refusal. So the record freezes
    the AND of the two, and says what the campaign actually allowed rather than
    what was asked for.
    """
    source = (REPO_ROOT / "scripts" / "nq_campaign.py").read_text()
    assert 'and bool(profile.get("fit_sessions", 0))' in source, (
        "registration freezes the config flag alone, so an unfitted campaign "
        "would let the agent spend trials on proposals the engine must refuse")


def test_the_config_default_is_on_and_the_gate_still_bites(tmp_path):
    """Turning the default on must not turn the gate off. A campaign that
    registered `False` -- which is every unfitted one -- still refuses."""
    import yaml

    cfg = yaml.safe_load((REPO_ROOT / "config" / "run.yaml").read_text())
    assert cfg["campaign"]["allow_fitted_proposals"] is True

    ledger = tmp_path / "ledger.jsonl"; ledger.write_text("")
    row = run_trial(FITTED, profile="harness", ledger=ledger,
                    campaign_id="c", index=0, allow_fitted=False)
    assert row["outcome"] == "refused_fitted"


# --------------------------------------------------------------------------- #
# seeding: optional, recorded, and checked before anything remote
# --------------------------------------------------------------------------- #
def test_a_seed_is_optional_and_the_record_says_which_happened():
    """Both branches are the design. A campaign may start from a human-written
    champion or from nothing, and a campaign that began from a working fitted
    strategy is not the same experiment as one that had to invent it."""
    rec = CampaignRecord(campaign_id="x", budget=3, profile="harness", q=0.05)
    assert rec.seed_path is None and rec.seed_digest is None
    # None is dropped from the digest, so records predating the field keep theirs
    d0 = rec.digest()
    rec2 = CampaignRecord(campaign_id="x", budget=3, profile="harness", q=0.05,
                          seed_path="src/strategies/viperq_amt.py",
                          seed_digest="abc123")
    assert rec2.digest() != d0


def test_the_seed_is_checked_against_the_profile_before_the_preflight():
    """A seed that cannot match the profile is pure local logic and must be
    refused for free -- not after a network round trip to the model, which is
    what the preflight is."""
    source = (REPO_ROOT / "scripts" / "nq_campaign.py").read_text()
    check = source.index("declares_fit(seed_text) != fitted_profile")
    preflight = source.index(".preflight()")
    assert check < preflight, (
        "the seed/profile check runs after the model preflight; a mismatched "
        "seed waits on the network before being refused")


def test_seed_is_refused_on_every_stage_but_register():
    source = (REPO_ROOT / "scripts" / "nq_campaign.py").read_text()
    assert "--seed applies to --stage register only" in source


def test_a_fitted_seed_into_an_unfitted_profile_is_refused_without_writing():
    """Verified end to end by subprocess so the ordering is real and not a
    source-grep promise. Must exit nonzero, fast, and leave no campaign dir."""
    import subprocess, time
    name = "zz-seed-test"
    d = REPO_ROOT / "runs" / "campaigns" / name
    assert not d.exists(), f"{d} already exists; pick another name"
    t0 = time.monotonic()
    r = subprocess.run(
        [sys.executable, "scripts/nq_campaign.py", "--stage", "register",
         "--name", name, "--profile", "nq-train",
         "--seed", "src/strategies/viperq_amt.py"],
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=30)
    assert r.returncode != 0
    assert time.monotonic() - t0 < 10, "the refusal waited on something remote"
    assert "has no fit window" in r.stdout + r.stderr
    assert not d.exists(), "a refused registration wrote a campaign directory"


# --------------------------------------------------------------------------- #
# N and the model are per-campaign decisions, frozen at registration
# --------------------------------------------------------------------------- #
def test_n_and_model_are_refused_on_every_stage_but_register():
    source = (REPO_ROOT / "scripts" / "nq_campaign.py").read_text()
    assert "--n and --model apply to --stage register only" in source


def test_run_and_report_read_the_model_from_the_record_not_the_constant():
    """Registering one model and running or auditing under another would make
    agent_config describe a campaign that never happened. Both later stages
    have to read it back."""
    source = (REPO_ROOT / "scripts" / "nq_campaign.py").read_text()
    assert source.count('(record.agent_config or {}).get("model", MODEL)') >= 2, (
        "run or report still uses the MODEL constant instead of the record")


def test_the_budget_flag_overrides_the_config_default():
    source = (REPO_ROOT / "scripts" / "nq_campaign.py").read_text()
    assert 'cc = {**cc, "n_trials": n_trials}' in source
    assert '"--n must be positive' in source
