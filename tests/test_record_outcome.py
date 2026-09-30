"""The feedback edge: what came of the paper that suggested the question.

No network. `outcome` was `null` on all 284 registry rows and always had been —
the pipeline could say what we decided about a paper and never what came of it,
which makes it a funnel rather than a loop.

Two assertions carry the weight, and the second is the one that matters:

1. A campaign's verdicts reach the row of the paper whose distillation produced
   its hypothesis, and only that paper.
2. **No score reaches the registry.** `rank_papers.py` is given campaign verdicts
   deliberately without scores, because ranking papers by resemblance to what
   scored well is selection on data already spent — and retrieval costs no trial
   count only because it never consults our market data. The registry is read by
   that ranker, so a score written here is an invisible back door into it.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(monkeypatch, tmp_path):
    """The module, pointed at a throwaway registry and campaign tree."""
    spec = importlib.util.spec_from_file_location(
        "record_outcome", REPO_ROOT / "scripts" / "record_outcome.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "REGISTRY", tmp_path / "registry.json")
    monkeypatch.setattr(mod, "CAMPAIGNS", tmp_path / "campaigns")
    return mod


def _campaign(root: pathlib.Path, name: str, *, hypothesis: str,
              gate: str = "ACCEPT", val_positive=None, tier3="CANNOT_TELL",
              committee=None, vetoed_by=()):
    d = root / "campaigns" / name
    d.mkdir(parents=True)
    (d / "campaign.json").write_text(json.dumps(
        {"record": {"campaign_id": name, "hypothesis_path": hypothesis}}))
    (d / "tier2-summary.json").write_text(json.dumps(
        {"gate": {"verdict": gate, "p": 0.0339, "observed_best": 0.2898}}))
    if val_positive is not None:
        (d / "validation.json").write_text(json.dumps(
            {"rule_a_sortino_positive": val_positive, "rule_c_decay": -0.3455,
             "validation": {"primary": -0.0557}}))
    if tier3:
        (d / "tier3-verdict.json").write_text(json.dumps({"verdict": tier3}))
    if committee:
        (d / "committee-verdict.json").write_text(json.dumps(
            {"verdict": committee, "vetoed_by": list(vetoed_by)}))
    return d


def _registry(root: pathlib.Path, **rows):
    papers = {}
    for pid, hyp in rows.items():
        papers[pid] = {"id": pid, "title": f"paper {pid}", "feasibility": "direct",
                       "hypothesis_path": hyp, "campaign_ids": [], "outcome": None}
    (root / "registry.json").write_text(json.dumps({"schema": "paper-registry/1",
                                                    "papers": papers}))


# --------------------------------------------------------------------------- #
# the edge itself
# --------------------------------------------------------------------------- #
def test_a_concluded_campaign_reaches_the_paper_that_suggested_it(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    _registry(tmp_path, **{"2605.04004v3": "config/hypotheses/ohlcv.md",
                           "2609.07989v1": "config/hypotheses/other.md"})
    _campaign(tmp_path, "nq-ml-07", hypothesis="config/hypotheses/ohlcv.md",
              gate="ACCEPT", val_positive=False, committee="VETO",
              vetoed_by=["statistics", "robustness"])

    v = mod.verdicts_for("nq-ml-07")
    reg = json.loads(mod.REGISTRY.read_text())
    assert mod.paper_for(reg, v["hypothesis"]) == "2605.04004v3"
    assert mod.paper_for(reg, "config/hypotheses/other.md") == "2609.07989v1"
    assert mod.paper_for(reg, "config/hypotheses/nobody.md") is None

    assert v["gate"] == "ACCEPT"
    assert v["committee"] == "VETO"
    assert v["vetoed_by"] == ["statistics", "robustness"]
    assert v["stopped_by"] == "committee"
    assert v["campaign_dir"].endswith("campaigns/nq-ml-07")


def test_no_score_reaches_the_registry():
    """**The line that matters.** The ranker reads the registry and is given
    verdicts without scores on purpose: picking papers that resemble what scored
    well is selection on data already spent, and retrieval is free of trial count
    only because it never consults our data. A score here is an invisible door
    into that.
    """
    src = (REPO_ROOT / "scripts" / "record_outcome.py").read_text()
    # the record's shape is a closed set, so adding a score means editing it
    assert 'FIELDS = ("campaign_id"' in src
    body = src[src.index("def verdicts_for"):src.index("def paper_for")]
    # the score-bearing keys of the files it reads, none of which it may pick up.
    # `rule_a_sortino_positive` is deliberately not on this list: it is a bool.
    for forbidden in ("observed_best", "rule_c_decay", "null_max", "primary",
                      '["p"]', 'get("p")'):
        assert forbidden not in body, f"{forbidden} would carry a score into the registry"
    assert "campaign_dir" in body, "the numbers are reachable by a human, by pointer"


def test_the_written_record_carries_no_numbers(monkeypatch, tmp_path):
    """Belt and braces: the assertion above reads the source, this one reads the
    artefact. The fixture's campaign files DO contain p, the best score and the
    decay, so anything leaking would show up here."""
    mod = _load(monkeypatch, tmp_path)
    _registry(tmp_path, **{"2605.04004v3": "config/hypotheses/ohlcv.md"})
    _campaign(tmp_path, "nq-ml-07", hypothesis="config/hypotheses/ohlcv.md",
              val_positive=False, committee="VETO", vetoed_by=["statistics"])
    v = mod.verdicts_for("nq-ml-07")
    text = json.dumps(v)
    for number in ("0.0339", "0.2898", "-0.3455", "-0.0557"):
        assert number not in text, f"{number} reached the registry record"
    assert set(v) == set(mod.FIELDS)

    # **The invariant, stated structurally rather than by substring.** Every value
    # is a string, a bool, a list of strings, or None. A float in this record is
    # a score by construction, whatever it is called.
    def leaves(x):
        if isinstance(x, dict):
            for y in x.values():
                yield from leaves(y)
        elif isinstance(x, (list, tuple)):
            for y in x:
                yield from leaves(y)
        else:
            yield x
    for leaf in leaves(v):
        assert not isinstance(leaf, float), f"{leaf!r} is a number in the record"
        assert isinstance(leaf, (str, bool, type(None))), f"{leaf!r}"


# --------------------------------------------------------------------------- #
# what it refuses to do
# --------------------------------------------------------------------------- #
def test_a_campaign_that_has_not_concluded_records_nothing(monkeypatch, tmp_path):
    """No gate verdict means the campaign is still running or its nulls have not
    been computed. That is not a result and saying so is correct."""
    mod = _load(monkeypatch, tmp_path)
    _registry(tmp_path, **{"2605.04004v3": "config/hypotheses/ohlcv.md"})
    d = tmp_path / "campaigns" / "nq-ml-08"
    d.mkdir(parents=True)
    (d / "campaign.json").write_text(json.dumps(
        {"record": {"hypothesis_path": "config/hypotheses/ohlcv.md"}}))
    assert mod.verdicts_for("nq-ml-08") is None


def test_a_human_written_hypothesis_has_no_paper_to_record_against(monkeypatch, tmp_path):
    """Every campaign run before today is in this state, and it is not an error.
    Twelve of them have concluded and none is traceable to a paper."""
    mod = _load(monkeypatch, tmp_path)
    _registry(tmp_path, **{"2605.04004v3": "config/hypotheses/ohlcv.md"})
    _campaign(tmp_path, "nq-mvp-01",
              hypothesis="config/hypotheses/intraday-continuation.md")
    reg = json.loads(mod.REGISTRY.read_text())
    v = mod.verdicts_for("nq-mvp-01")
    assert v is not None
    assert mod.paper_for(reg, v["hypothesis"]) is None


def test_survived_all_stages_is_not_a_promotion(monkeypatch, tmp_path):
    """No verdict in this project means approval. `conclusion` is a description of
    what stopped the candidate, and when nothing did, promotion is still a human
    decision taken elsewhere."""
    mod = _load(monkeypatch, tmp_path)
    _registry(tmp_path, **{"2605.04004v3": "config/hypotheses/ohlcv.md"})
    _campaign(tmp_path, "nq-ml-09", hypothesis="config/hypotheses/ohlcv.md",
              gate="ACCEPT", val_positive=True, committee="PASS")
    v = mod.verdicts_for("nq-ml-09")
    assert v["stopped_by"] is None
    assert "promot" not in json.dumps(v).lower()

    src = (REPO_ROOT / "scripts" / "record_outcome.py").read_text()
    assert "does not update the falsified-families list" in src
    assert "It does not decide promotion" in src


@pytest.mark.parametrize("gate,val,com,expected", [
    ("REJECT", None, None, "gate"),
    ("ACCEPT", False, None, None),          # validation is NOT consulted here
    ("ACCEPT", True, "VETO", "committee"),
    ("ACCEPT", True, "PASS", None),
    ("ACCEPT", None, None, None),
])
def test_stopped_by_names_only_the_train_side_gate_and_the_committee(
        monkeypatch, tmp_path, gate, val, com, expected):
    mod = _load(monkeypatch, tmp_path)
    _registry(tmp_path, **{"p": "config/hypotheses/h.md"})
    _campaign(tmp_path, "c", hypothesis="config/hypotheses/h.md", gate=gate,
              val_positive=val, committee=com)
    assert mod.verdicts_for("c")["stopped_by"] == expected


def test_recording_twice_replaces_rather_than_duplicates(monkeypatch, tmp_path):
    """Re-running the report stage must be idempotent."""
    mod = _load(monkeypatch, tmp_path)
    _registry(tmp_path, **{"2605.04004v3": "config/hypotheses/ohlcv.md"})
    _campaign(tmp_path, "nq-ml-07", hypothesis="config/hypotheses/ohlcv.md",
              val_positive=False, committee="VETO")
    import sys
    argv = sys.argv
    try:
        for _ in range(2):
            sys.argv = ["record_outcome.py", "--campaign", "nq-ml-07"]
            mod.main()
    finally:
        sys.argv = argv
    row = json.loads(mod.REGISTRY.read_text())["papers"]["2605.04004v3"]
    assert row["campaign_ids"] == ["nq-ml-07"]
    assert len(row["outcome"]) == 1


# --------------------------------------------------------------------------- #
# the wiring
# --------------------------------------------------------------------------- #
def test_the_report_stage_records_the_outcome_without_being_asked():
    """**Stage 0b was a standalone command nobody triggered, and the corpus went
    stale silently.** A feedback edge that has to be remembered will not happen,
    so the report stage calls it -- on the committee's success path and on its
    failure path, because a campaign whose committee errored still concluded."""
    src = (REPO_ROOT / "scripts" / "nq_campaign.py").read_text()
    report = src[src.index("def stage_report"):]
    assert report.count("_record_outcome(name)") == 2, (
        "both the committee-failed path and the normal path must record")
    helper = src[src.index("def _record_outcome"):src.index("def stage_report")]
    assert "except Exception" in helper, (
        "a registry write must never cost a campaign its report")
    assert "record_outcome.py" in helper, "one implementation, not two"


def test_no_validation_result_reaches_the_registry():
    """**Corrected 2026-09-29**, after the first version recorded
    `validation_positive`.

    §4: "Campaign k+1 may depend on prior campaigns' train results -- never on
    their validation results. Choosing the next hypothesis by what did well on
    validation is selecting on the thing being tested." The ranker reads this
    registry, so a validation result here is the same kind of door a score would
    be.

    Necessary and not sufficient: the committee verdict is itself
    validation-informed, because the report stage hands the auditors a summary
    with the validation numbers in it. The real protection is that the ranker
    never reads a paper's `outcome` -- asserted in test_rank_papers.py.
    """
    src = (REPO_ROOT / "scripts" / "record_outcome.py").read_text()
    assert "validation_positive" not in mod_fields(), "the field is gone"
    body = src[src.index("def verdicts_for"):src.index("def paper_for")]
    assert "validation.json" not in body, "it does not even open the file"
    assert "rule_a_sortino_positive" not in body


def mod_fields():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "record_outcome", REPO_ROOT / "scripts" / "record_outcome.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.FIELDS
