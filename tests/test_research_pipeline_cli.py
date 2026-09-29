"""The corpus pipeline end to end, offline: fetch -> screen -> rerank -> distil.

Drives `scripts/research_pipeline.py` main() against a temp registry root, with
the network and the models replaced the way every other offline test replaces
them: `fetch_feed` by fake papers, the screen agent and the distil agent by
fakes, and the rerank left real -- it is the one stage with no model at all.
Letting the real stages talk to the real registry is the point: the chain is
only worth the paper if end-to-end `pending` flow works as a whole.

`register` is not exercised here: it writes a CampaignRecord tied to the real
`config/hypotheses/`, a git worktree and a model preflight, none of which is an
offline affair.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.core.loop import research  # noqa: E402
from src.core.loop.registry import Registry  # noqa: E402
from src.core.loop.research import Paper  # noqa: E402

GRU_PAPER = Paper(
    arxiv_id="2606.11111v1", title="A gated GRU confirmation two-stage model "
    "for NQ closes",
    authors=("G. Writer",), published="2026-06-01",
    abstract="We stack a recurrent sequence model with a gating decision "
             "layer to confirm the next open's direction.",
    categories=("q-fin.TR",))

CLOSE_PAPER = Paper(
    arxiv_id="2606.22222", title="Closing pressure and the next open",
    authors=("C. Writer",), published="2026-06-02",
    abstract="We study how the volume imbalance at the close relates to the "
             "direction of the next open, on NQ intraday data.",
    categories=("q-fin.ST",))


def _page() -> str:
    paras = [
        "One account says the imbalance at the close is absorbed overnight, so "
        "the next open is a fair continuation of the close. The competing "
        "account says the absorption is only partial, and the part of the book "
        "that did not finish before the close leaks into the next session's "
        "opening bars.",
        "An observation that discriminates them is a session whose closing "
        "imbalance is large relative to that session's own ordinary range: "
        "continuation says the open tracks the close; the partial-absorption "
        "account says the direction gives some of it back, in the first few "
        "bars after the open.",
        "What is deliberately NOT being asked is whether the imbalance can be "
        "forecast. Only what a realised closing imbalance means for the next "
        "open is in question, with no side information beyond the bar series, "
        "the volume and the close.",
    ]
    body = "\n\n".join(paras)
    s = body
    while len(s) < 4000:
        s += "\n\n" + body
    return "# Does closing pressure predict the next open?\n\n" + s[:4200]


@pytest.fixture
def cli(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "research_pipeline_ut", REPO_ROOT / "scripts" / "research_pipeline.py")
    rp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rp)

    monkeypatch.setattr(research, "fetch_feed", lambda **kw: [GRU_PAPER, CLOSE_PAPER])

    class FakeScreen:
        def __init__(self, **kw):
            self.paper = kw["paper"]
            self.contract = kw["contract"]

        def config(self):
            return {"implementation": "tests.FakeScreen", "paper": self.paper.arxiv_id}

        def drain_log(self):
            return {"n_calls": 1, "repair_attempts": 0, "prompt_tokens": 10,
                    "completion_tokens": 5, "latency_s": 0.1, "blocked": False,
                    "models_served": ["test-model"]}

        def preflight(self):
            return {"requested": "test-model", "served": "test-model"}

        def screen(self):
            from src.core.loop.research import ScreenVerdict
            return ScreenVerdict("direct", "claim matches the contract",
                                 adaptation="", quote="q")

    class FakeDistil:
        def __init__(self, **kw):
            self.paper = kw["paper"]
            self.brief = kw["brief"]
            self.run_facts = kw["run_facts"]
            self.data_doc = kw["data_doc"]

        def config(self):
            return {"implementation": "tests.FakeDistil",
                    "paper": self.paper.arxiv_id,
                    "data_doc": self.data_doc}

        def drain_log(self):
            return {"n_calls": 1, "repair_attempts": 0, "prompt_tokens": 10,
                    "completion_tokens": 5, "latency_s": 0.1, "blocked": False,
                    "models_served": ["test-model"]}

        def preflight(self):
            return {"requested": "test-model", "served": "test-model"}

        def distil(self):
            return _page()

    monkeypatch.setattr(research, "ScreenAgent", FakeScreen)
    monkeypatch.setattr(research, "DistilAgent", FakeDistil)
    return rp


def test_pipeline_moves_papers_through_pending(tmp_path, cli):
    root = tmp_path / "corpus"
    rp = cli
    assert rp.main(["fetch", "--root", str(root)]) == 0
    assert rp.main(["screen", "--root", str(root)]) == 0
    assert rp.main(["rerank", "--root", str(root)]) == 0

    reg = Registry(root=root, year=2026)
    assert reg.pending("distil") == ["2606.22222"], (
        "the GRU paper is demoted (falsified family), only the close paper "
        "reaches the distil")

    assert rp.main(["distil", "--root", str(root)]) == 0
    assert reg.pending("register") == ["2606.22222"]
    assert (root / "distills" / "2606.22222.md").is_file()
    assert (root / "distills" / "2606.22222.json").is_file()

    # The demoted paper is a corpus row all the way down, with its citation.
    gru = reg.state("2606.11111")
    assert gru["feasibility"] == "direct"
    assert gru["demoted"] is True
    assert "docs/datasets/nq-es-qqq.md" in gru["demoted_reason"]
    assert "hypothesis_path" not in gru


def test_view_round_trips_the_json(tmp_path, cli):
    root = tmp_path / "corpus"
    rp = cli
    rp.main(["fetch", "--root", str(root)])
    rp.main(["screen", "--root", str(root)])
    rp.main(["rerank", "--root", str(root)])
    rp.main(["distil", "--root", str(root)])

    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert rp.main(["view", "--root", str(root), "--json"]) == 0
    blob = json.loads(buf.getvalue())
    keys = {p["key"] for p in blob["papers"]}
    assert keys == {"2606.11111", "2606.22222"}
    assert blob["pending"] == {
        "screen": [], "rerank": [], "distil": [], "register": ["2606.22222"]}
    by_key = {p["key"]: p for p in blob["papers"]}
    assert by_key["2606.22222"]["draft_digest"]
    assert by_key["2606.22222"]["hypothesis_path"].endswith(
        "distills/2606.22222.md")


def _advance_to_distil(rp, root):
    assert rp.main(["fetch", "--root", str(root)]) == 0
    assert rp.main(["screen", "--root", str(root)]) == 0
    assert rp.main(["rerank", "--root", str(root)]) == 0


def test_distil_must_disclose_a_prior_rejection(tmp_path, cli):
    """A page that ignores a trial the corpus already turned down is refused,
    on the file, before it is written."""
    root = tmp_path / "corpus"
    rp = cli
    _advance_to_distil(rp, root)
    reg = Registry(root=root, year=2026)
    reg.append(kind="outcome", arxiv_id="2606.99999", verdict="rejected",
               reason="landed near zero")

    with pytest.raises(SystemExit):
        rp.main(["distil", "--root", str(root)])
    assert not (root / "distills" / "2606.22222.md").exists()
    failed = (root / "distills" / "2606.22222.failed.log").read_text()
    assert "names none" in failed
    assert "2606.99999" in failed


def test_distil_records_the_rejections_it_disclosed(tmp_path, cli, monkeypatch):
    """A page that names a real prior rejection passes and the citation is
    recorded, so the corpus can see what was carried forward."""
    root = tmp_path / "corpus"
    rp = cli
    _advance_to_distil(rp, root)
    reg = Registry(root=root, year=2026)
    reg.append(kind="outcome", arxiv_id="2606.99999", verdict="rejected",
               reason="landed near zero")

    class DisclosingDistil:
        def __init__(self, **kw):
            self.paper = kw["paper"]

        def config(self):
            return {"implementation": "tests.DisclosingDistil"}

        def drain_log(self):
            return {"n_calls": 1, "repair_attempts": 0, "prompt_tokens": 10,
                    "completion_tokens": 5, "latency_s": 0.1, "blocked": False,
                    "models_served": ["test-model"]}

        def distil(self):
            return _page() + "\n\nprior_invalidation_rejected: 2606.99999\n"

    monkeypatch.setattr(research, "DistilAgent", DisclosingDistil)
    assert rp.main(["distil", "--root", str(root)]) == 0
    rec = json.loads((root / "distills" / "2606.22222.json").read_text())
    assert rec["prior_rejections_cited"] == ["2606.99999"]


def test_research_stage_chains_the_whole_prompt_run(tmp_path, cli, capsys):
    root = tmp_path / "corpus"
    rp = cli
    prompt = "Does closing pressure predict the next open?"
    assert rp.main(["research", "--root", str(root),
                    "--prompt", prompt]) == 0
    out = capsys.readouterr().out
    reg = Registry(root=root, year=2026)
    assert reg.pending("register") == ["2606.22222"]
    close = reg.state("2606.22222")
    assert close["relevance"] >= 1           # scored against the prompt
    assert close["prompt"] == prompt
    assert "prompt run complete" in out
    assert "candidate" in out
    gru = reg.state("2606.11111")
    assert gru["demoted"] is True            # the unrelated family demotes
    assert gru["relevance"] < close["relevance"]   # the prompt ranks the close paper above the GRU paper