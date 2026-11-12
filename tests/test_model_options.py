"""Every agent part's model can be chosen, reaches its own call, and is recorded.

2026-11-12: the judge, the writer's paper picker, screening, ranking, the
between-campaign diagnosis, and each auditor separately. No network.
"""
from __future__ import annotations

import argparse
import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def _load(name):
    spec = importlib.util.spec_from_file_location(f"{name}_mo", ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _src(name):
    return (ROOT / "scripts" / f"{name}.py").read_text()


# --------------------------------------------------------------------------- #
# the committee: one model, or one per auditor
# --------------------------------------------------------------------------- #
@pytest.fixture()
def committee(monkeypatch):
    import importlib
    # `audit.committee` re-exports a function named `review`, so the plain
    # `import audit.committee.review as R` binds the function, not the module.
    R = importlib.import_module("audit.committee.review")
    seen = {}

    class FakeClient:
        def __init__(self, model, **k):
            self.model, self.calls = model, [{"model_served": model}]

    def one(axis, client, *a):
        seen[axis] = client.model
        return R.AuditorVerdict(axis=axis, verdict="CANNOT_TELL")

    monkeypatch.setattr(R, "Client", FakeClient)
    monkeypatch.setattr(R, "preflight", lambda m: m)
    monkeypatch.setattr(R, "_one", one)
    monkeypatch.setattr(R, "prompt_digests", lambda: {})
    return R, seen


def test_each_auditor_can_run_on_its_own_model(committee):
    R, seen = committee
    cv = R.review({}, source=None, model="m-default", proposer_model="p",
                  auditor_models={"leakage": "m-leak"})
    assert seen == {"leakage": "m-leak", "statistics": "m-default",
                    "economics": "m-default", "robustness": "m-default"}
    assert cv.auditor_models["leakage"] == "m-leak"
    assert cv.as_dict()["auditor_models"]["statistics"] == "m-default"
    assert len(cv.usage) == 2                      # one entry per distinct client


def test_without_overrides_nothing_extra_is_recorded(committee):
    R, seen = committee
    cv = R.review({}, source=None, model="m-default", proposer_model="p")
    assert set(seen.values()) == {"m-default"} and cv.auditor_models == {}


def test_one_auditor_on_the_proposers_model_is_not_independent(committee):
    R, _ = committee
    cv = R.review({}, source=None, model="m-default", proposer_model="deepseek",
                  auditor_models={"economics": "deepseek"})
    assert cv.independent is False
    cv = R.review({}, source=None, model="m-default", proposer_model="deepseek")
    assert cv.independent is True


def test_a_misspelt_auditor_is_refused(committee):
    R, _ = committee
    with pytest.raises(ValueError, match="no auditor named"):
        R.review({}, source=None, auditor_models={"leakge": "m"})


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #
def test_auditor_model_flag_parses_axis_equals_model():
    nq = _load("nq_campaign")
    assert nq._axis_model("leakage=gemini-2.5-pro") == {"leakage": "gemini-2.5-pro"}
    for bad in ("leakage", "=m", "leakage="):
        with pytest.raises(argparse.ArgumentTypeError):
            nq._axis_model(bad)


def test_register_freezes_the_judge_picker_and_auditor_models():
    src = _src("nq_campaign")
    reg = src.split("def stage_register", 1)[1].split("\ndef ", 1)[0]
    assert '"model": judge_model or fidelity_judge.MODEL' in reg
    assert 'agent_cfg["auditor_models"]' in reg
    assert "picker_model=picker_model" in reg
    assert "--auditor-model names no auditor" in reg       # refused at register
    assert '(record.agent_config or {}).get("auditor_models")' in src
    for flag in ('"--judge-model"', '"--picker-model"', '"--auditor-model"'):
        assert flag in src


def test_model_flags_are_refused_on_later_stages(tmp_path):
    import subprocess
    r = subprocess.run([sys.executable, "scripts/nq_campaign.py", "--stage", "run",
                        "--name", "zz", "--judge-model", "m"],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode != 0 and "apply to --stage register only" in r.stdout + r.stderr


def test_the_picker_model_reaches_the_paper_picker(monkeypatch, tmp_path):
    wh = _load("write_hypothesis")
    asked = {}
    monkeypatch.setenv("PRACTICUM_RUNS_ROOT", str(tmp_path))
    monkeypatch.setattr(wh, "pick_papers", lambda q, model=None, **k: asked.setdefault("m", model) and [])
    monkeypatch.setattr(wh.dp, "campaign_in_flight", lambda: "")
    monkeypatch.setattr(wh.dp, "_client", lambda m: (_ for _ in ()).throw(
        wh.Unattended("stop after the picker")))
    with pytest.raises(wh.Unattended):
        wh.write_unattended("q", campaign="c", picker_model="picker-m")
    assert asked["m"] == "picker-m"


# --------------------------------------------------------------------------- #
# papers: screening, ranking, retrieval
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["screen_papers", "rank_papers"])
def test_screening_and_ranking_take_a_model(name):
    import subprocess
    out = subprocess.run([sys.executable, f"scripts/{name}.py", "--help"],
                         cwd=ROOT, capture_output=True, text=True).stdout
    assert "--model MODEL" in out
    assert "global MODEL" in _src(name) and "MODEL = a.model" in _src(name)


def test_retrieve_passes_the_paper_models_to_its_stages():
    src = _src("retrieve")
    assert '["--model", a.screen_model] if a.screen_model' in src
    assert '["--model", a.rank_model] if a.rank_model' in src


def test_fetching_uses_the_paper_model(monkeypatch):
    import outcome_retrieval as orr
    seen = {}
    monkeypatch.setattr(orr.subprocess, "run",
                        lambda argv, cwd: seen.setdefault("argv", argv) and type("R", (), {"returncode": 0}))
    orr.fetch_new([{"query": "q"}], ROOT / "x.md", paper_model="paper-m")
    argv = seen["argv"]
    assert argv[argv.index("--screen-model") + 1] == "paper-m"
    assert argv[argv.index("--rank-model") + 1] == "paper-m"


def test_the_refiner_diagnoses_on_the_retrieval_model_and_survives_its_loss():
    src = _src("refine_hypothesis")
    assert "retrieval_model = a.retrieval_model or a.model" in src
    assert "Client(model=retrieval_model" in src
    assert "paper_model=a.paper_model" in src
    # a substituted retrieval model falls back to the overlap choice, recorded
    assert "except (LLMUnavailable, ModelSubstituted, ValueError," in src


def test_the_commit_says_when_retrieval_did_not_run_and_on_what_it_did():
    import outcome_retrieval as orr
    assert "did not run (ModelSubstituted" in orr.commit_lines(
        {"problems": [], "papers": [], "error": "ModelSubstituted: x"})
    found = {"problems": [{"what": "w", "query": "q"}], "papers": [],
             "retrieval_model": "diag-m"}
    assert "auditors only, on diag-m)" in orr.commit_lines(found)


# --------------------------------------------------------------------------- #
# session plans
# --------------------------------------------------------------------------- #
def test_a_plan_can_set_every_model():
    src = _src("research_session")
    for needle in ('("--judge-model", "judge_model")', '("--picker-model", "picker_model")',
                   '["--auditor-model", f"{axis}={m}"]', '["--retrieval-model"',
                   '["--paper-model"', '"auditor_models": fam.get("auditor_models")'):
        assert needle in src, needle
    reh = _load("rehearse")
    for k in ("picker_model", "retrieval_model", "paper_model", "judge_model"):
        assert k in reh.MODEL_KEYS
