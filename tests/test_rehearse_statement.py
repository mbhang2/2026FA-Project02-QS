"""rehearse.py: a family from a statement, with the models an endpoint serves."""
from __future__ import annotations

import importlib.util
import json
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _reh():
    spec = importlib.util.spec_from_file_location("rehearse_t", ROOT / "scripts" / "rehearse.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_rehearsal_plan_can_start_from_a_statement(tmp_path):
    reh = _reh()
    p = reh._plan(tmp_path / "p.yaml", campaigns=3, n=4, width=2, seed=None,
                  model="m", seed_question="CVD: divergence, reversal",
                  models={"writer_model": "w", "audit_model": "a", "reviewer_model": None})
    fam = yaml.safe_load(p.read_text())["family"]
    assert fam["seed_question"] == "CVD: divergence, reversal"   # quoted safely
    assert "seed" not in fam
    assert fam["writer_model"] == "w" and fam["audit_model"] == "a"
    assert "reviewer_model" not in fam                           # unset stays unset


def _camp(root, name, path, origin=None, verdict="VETO"):
    d = root / "campaigns" / name
    d.mkdir(parents=True)
    (d / "campaign.json").write_text(json.dumps({"record": {
        "profile": "rehearsal", "budget": 0, "hypothesis_path": path,
        "agent_config": {"hypothesis_origin": origin}}}))
    (d / "committee-verdict.json").write_text(json.dumps({"verdict": verdict, "error": "x"}))


def test_the_checks_catch_what_this_rehearsal_exists_to_prove(tmp_path, monkeypatch):
    reh = _reh()
    monkeypatch.setattr(reh, "SCRATCH", tmp_path)
    _camp(tmp_path, "reh-01", "h/a.md", origin=None, verdict="ERROR")
    _camp(tmp_path, "reh-02", "h/a.md")
    bad = "\n".join(reh.check(2, "I want CVD"))
    assert "reh-01: the committee failed to run" in bad
    assert "reh-01: not registered from the statement" in bad
    assert "campaigns share a question file" in bad


def test_a_clean_statement_family_passes_those_checks(tmp_path, monkeypatch):
    reh = _reh()
    monkeypatch.setattr(reh, "SCRATCH", tmp_path)
    _camp(tmp_path, "reh-01", "h/a.md",
          origin={"machine_written": True, "statement": "I want CVD"})
    _camp(tmp_path, "reh-02", "h/b.md")
    bad = "\n".join(reh.check(2, "I  want CVD"))
    assert "statement" not in bad and "share a question" not in bad
    assert "committee" not in bad


def test_empty_nulls_are_a_defect_only_when_a_trial_was_scored(tmp_path, monkeypatch):
    reh = _reh()
    monkeypatch.setattr(reh, "SCRATCH", tmp_path)
    for name, score in (("reh-01", {"primary": None, "rejected": True}),
                        ("reh-02", {"primary": 0.1})):
        _camp(tmp_path, name, f"h/{name}.md")
        d = tmp_path / "campaigns" / name
        (d / "ledger.jsonl").write_text(json.dumps({"purpose": "check", "score": score}) + "\n")
        (d / "nulls.json").write_text(json.dumps({"samples": [[]]}))
    bad = "\n".join(reh.check(2))
    assert "reh-01: every null sample is EMPTY" not in bad      # nothing scored
    assert "reh-02: every null sample is EMPTY" in bad          # the merge bug
