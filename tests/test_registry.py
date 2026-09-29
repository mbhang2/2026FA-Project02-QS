"""The corpus registry: registered once, decided once, never overwritten.

Covers `src/core/loop/registry.py`. The three properties that matter, each
asserted against a real file layout in a temp directory:

1. `register` writes the paper row once and a re-register -- even after the row
   was tampered -- leaves the file byte-for-byte alone. Overwrite is the
   failure mode the registry exists to prevent.
2. Decisions (`screen`, `rerank`, `distil`, `register`, `outcome`) are
   one-shot: a second `append` raises `RegistryConflict`, and `state` merges
   with first-wins semantics across every year's log, not just the current one.
3. `pending` classifies the corpus into exactly the papers ready for each
   stage, so a pipeline stage is a filter over the registry rather than a list
   a script keeps somewhere else.
"""

from __future__ import annotations

import json

import pytest

from src.core.loop.registry import (
    KINDS,
    Registry,
    RegistryConflict,
    RegistryError,
    key_of,
)
from src.core.loop.research import Paper

PAPER_A = Paper(
    arxiv_id="2601.10001v1", title="Option-Delta Order Flow",
    authors=("A. Writer",), published="2026-01-05", abstract="A paper.",
    categories=("q-fin.TR",),
)
PAPER_B = Paper(
    arxiv_id="2601.20002", title="Clustering of Trades",
    authors=("B. Writer",), published="2026-01-06", abstract="Another paper.",
    categories=("q-fin.ST",),
)


def _reg(tmp_path) -> Registry:
    return Registry(root=tmp_path, year=2026)


# --------------------------------------------------------------------------- #
# the paper row
# --------------------------------------------------------------------------- #

def test_key_of_strips_an_arith_version_and_ignores_everything_else():
    assert key_of("2401.12345v4") == "2401.12345"
    assert key_of("2401.12345") == "2401.12345"
    assert key_of("2502.99999v1") == "2502.99999"
    assert key_of("abc") == "abc"


def test_register_writes_the_row_once(tmp_path):
    reg = _reg(tmp_path)
    created, record = reg.register(PAPER_A, "cat:q-fin.TR")
    assert created is True
    assert record["key"] == "2601.10001"
    assert record["arxiv_id"] == PAPER_A.arxiv_id
    assert record["title"] == PAPER_A.title
    assert reg.is_registered("2601.10001v1")
    assert reg.is_registered("2601.10001v3")


def test_re_register_never_overwrites_even_a_tampered_row(tmp_path):
    reg = _reg(tmp_path)
    reg.register(PAPER_A, "cat:q-fin.TR")
    path = reg.paper_path("2601.10001")
    tampered = json.loads(path.read_text())
    tampered["title"] = "a rewritten history"
    path.write_text(json.dumps(tampered, indent=2, sort_keys=True) + "\n")
    before = path.read_bytes()

    created, record = reg.register(PAPER_A, "cat:q-fin.TR")

    assert created is False
    assert record["title"] == "a rewritten history"
    assert path.read_bytes() == before, (
        "a re-register rewrote a tampered row; the registry never overwrites")


def test_register_two_papers_under_two_rows(tmp_path):
    reg = _reg(tmp_path)
    reg.register(PAPER_A, "cat:q-fin.TR")
    created, _ = reg.register(PAPER_B, "cat:q-fin.ST")
    assert created is True
    assert sorted(p.stem for p in (tmp_path / "papers").glob("*.json")) == [
        "2601.10001", "2601.20002"]


# --------------------------------------------------------------------------- #
# the event log
# --------------------------------------------------------------------------- #

def test_append_writes_one_json_line_and_events_read_it_back(tmp_path):
    reg = _reg(tmp_path)
    reg.append(kind="screen", arxiv_id=PAPER_A.arxiv_id,
               feasibility="adaptable", feasibility_reason="needs a bar",
               adaptation="close bars")
    lines = (tmp_path / "corpus-2026.jsonl").read_text().splitlines()
    assert len(lines) == 1
    line = json.loads(lines[0])
    assert line["kind"] == "screen"
    assert line["arxiv_id"] == "2601.10001"
    assert line["feasibility"] == "adaptable"

    evs = reg.events("2601.10001v9")
    assert len(evs) == 1
    assert evs[0]["feasibility"] == "adaptable"


def test_append_rejects_a_second_decision_of_the_same_kind(tmp_path):
    reg = _reg(tmp_path)
    reg.append(kind="screen", arxiv_id="2601.10001",
               feasibility="infeasible", feasibility_reason="no data")
    with pytest.raises(RegistryConflict):
        reg.append(kind="screen", arxiv_id="2601.10001",
                   feasibility="adaptable", feasibility_reason="reconsider")
    assert len(reg.events("2601.10001")) == 1


def test_two_papers_may_each_have_their_own_screen(tmp_path):
    reg = _reg(tmp_path)
    reg.append(kind="screen", arxiv_id="2601.10001", feasibility="infeasible",
               feasibility_reason="one")
    reg.append(kind="screen", arxiv_id="2601.20002", feasibility="direct",
               feasibility_reason="two")
    assert len(reg.events()) == 2


def test_append_rejects_an_unknown_kind(tmp_path):
    reg = _reg(tmp_path)
    with pytest.raises(RegistryError):
        reg.append(kind="draft", arxiv_id="2601.10001", x=1)


def test_the_kinds_are_fetch_plus_the_one_shots():
    assert KINDS == ("fetch", "screen", "rerank", "distil", "register", "outcome")


# --------------------------------------------------------------------------- #
# state and pending
# --------------------------------------------------------------------------- #

def test_state_merges_row_and_events_first_wins(tmp_path):
    reg = _reg(tmp_path)
    reg.register(PAPER_A, "cat:q-fin.TR")
    reg.append(kind="screen", arxiv_id=PAPER_A.arxiv_id, feasibility="adaptable",
               feasibility_reason="needs a bar", adaptation="close bars")
    reg.append(kind="rerank", arxiv_id=PAPER_A.arxiv_id, demoted=False,
               demoted_reason="", relevance=0)
    st = reg.state("2601.10001")
    assert st["title"] == PAPER_A.title
    assert st["feasibility"] == "adaptable"
    assert st["demoted"] is False
    assert st["relevance"] == 0


def test_pending_classifies_the_corpus_by_stage(tmp_path):
    reg = _reg(tmp_path)
    reg.register(PAPER_A, "cat:q-fin.TR")
    reg.register(PAPER_B, "cat:q-fin.ST")

    assert reg.pending("screen") == ["2601.10001", "2601.20002"]

    reg.append(kind="screen", arxiv_id="2601.10001", feasibility="direct",
               feasibility_reason="right shape")
    reg.append(kind="screen", arxiv_id="2601.20002", feasibility="infeasible",
               feasibility_reason="no data")
    assert reg.pending("screen") == []
    assert reg.pending("rerank") == ["2601.10001"]

    reg.append(kind="rerank", arxiv_id="2601.10001", demoted=False,
               demoted_reason="", relevance=0)
    assert reg.pending("rerank") == []
    assert reg.pending("distil") == ["2601.10001"]

    reg.append(kind="distil", arxiv_id="2601.10001",
               hypothesis_path="config/hypotheses/feed-2601.10001.md",
               draft_digest="0" * 64)
    assert reg.pending("distil") == []
    assert reg.pending("register") == ["2601.10001"]


def test_demoted_papers_never_reach_distil(tmp_path):
    reg = _reg(tmp_path)
    reg.register(PAPER_A, "cat:q-fin.TR")
    reg.register(PAPER_B, "cat:q-fin.ST")
    reg.append(kind="screen", arxiv_id="2601.10001", feasibility="adaptable",
               feasibility_reason="needs a bar", adaptation="close bars")
    reg.append(kind="screen", arxiv_id="2601.20002", feasibility="direct",
               feasibility_reason="right shape")
    reg.append(kind="rerank", arxiv_id="2601.10001", demoted=True,
               demoted_reason="falsified family", relevance=0)
    reg.append(kind="rerank", arxiv_id="2601.20002", demoted=False,
               demoted_reason="", relevance=0)
    assert reg.pending("distil") == ["2601.20002"]


def test_pending_rejects_an_unknown_stage(tmp_path):
    with pytest.raises(RegistryError):
        _reg(tmp_path).pending("taxonomise")


def test_one_shot_is_global_across_year_logs(tmp_path):
    reg_2026 = Registry(root=tmp_path, year=2026)
    reg_2026.append(kind="screen", arxiv_id="2601.10001", feasibility="direct",
                    feasibility_reason="one")
    reg_2027 = Registry(root=tmp_path, year=2027)
    with pytest.raises(RegistryConflict):
        reg_2027.append(kind="screen", arxiv_id="2601.10001",
                        feasibility="adaptable", feasibility_reason="again")
    assert len(reg_2027.events("2601.10001")) == 1
    assert len(reg_2027.events()) == 1


def test_a_corrupt_event_line_fails_loudly(tmp_path):
    reg = _reg(tmp_path)
    reg.append(kind="screen", arxiv_id="2601.10001", feasibility="direct",
               feasibility_reason="one")
    log = tmp_path / "corpus-2026.jsonl"
    log.write_text(log.read_text() + "not json\n")
    with pytest.raises(RegistryError):
        reg.events()


def test_register_refuses_to_overwrite_a_corrupt_row(tmp_path):
    reg = _reg(tmp_path)
    reg.register(PAPER_A, "cat:q-fin.TR")
    reg.paper_path("2601.10001").write_text("{nope")
    with pytest.raises(RegistryError):
        reg.register(PAPER_A, "cat:q-fin.TR")