"""The retrieval pipeline as one command, and the line it stops at.

No network. What matters is where it stops and what it refuses, because the
stages themselves are tested in their own files.

It **ranks and stops.** That is Amendment 8 rather than caution: a machine may
write a hypothesis, but the commit is the human act and it is where §6a's
feasibility sanity check actually happens. A pipeline that fetched, ranked,
distilled and registered in one command would put a model's judgement about what
is worth several hundred looks at the data beyond review.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (REPO_ROOT / "scripts" / "retrieve.py").read_text()
FLAT = " ".join(SRC.split())


def _mod():
    spec = importlib.util.spec_from_file_location(
        "retrieve", REPO_ROOT / "scripts" / "retrieve.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# where it stops
# --------------------------------------------------------------------------- #
def test_it_cannot_register_a_campaign():
    """The one hard line. Everything upstream of a campaign costs no trial count
    precisely because none of it looks at our data; a driver that could register
    would spend N as a side effect of tidying the corpus."""
    assert "nq_campaign" not in SRC, "it must not be able to register anything"
    assert "--stage" not in SRC
    assert "cannot run a campaign" in FLAT


def test_it_distils_only_when_a_paper_is_named_and_then_stops():
    """Stage 3 is opt-in, one paper, and the output is a file nobody has committed
    yet."""
    assert '"--distil"' in SRC
    assert "if a.distil:" in SRC
    assert "NOTHING was registered" in SRC
    assert "the commit is the pre-registration (Amendment 8)" in FLAT


def test_the_default_path_distils_nothing():
    assert "ranked and stopped. Nothing was distilled and nothing registered." in SRC


def test_the_stage_order_is_fetch_screen_rank():
    """Screen before rank, because the screen is what decides a paper is worth
    downloading a body for -- and it fetches that body itself."""
    f = SRC.index("stage 0  fetch")
    s = SRC.index("stage 1  screen")
    r = SRC.index("stage 2  rank")
    d = SRC.index("stage 3  distil")
    assert f < s < r < d
    assert "stage 0b bodies" in SRC, "the screen fetches bodies; say so in the log"


def test_a_failed_stage_stops_the_ones_after_it():
    """Ranking a half-screened corpus, or screening nothing because the fetch
    failed, produces judgements nobody should trust."""
    assert "fetch failed; nothing downstream ran" in SRC
    assert "screen failed; ranking skipped" in SRC


# --------------------------------------------------------------------------- #
# the live question
# --------------------------------------------------------------------------- #
def test_the_current_question_is_passed_through_to_the_ranker():
    assert '"--asking"' in SRC
    assert 'argv += ["--asking", str(a.asking)]' in SRC


def test_the_absence_of_a_current_question_is_reported_not_hidden():
    """Ranking with no live question is legitimate -- it is what produced the
    two-axis re-rank -- but the record should say which happened."""
    assert "NO current question" in SRC
    proc = subprocess.run(
        [sys.executable, "scripts/retrieve.py", "--dry-run"],
        capture_output=True, text=True, cwd=str(REPO_ROOT))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "NO current question" in proc.stdout


def test_a_missing_hypothesis_is_refused_before_anything_runs():
    proc = subprocess.run(
        [sys.executable, "scripts/retrieve.py", "--asking", "config/nope.md"],
        capture_output=True, text=True, cwd=str(REPO_ROOT))
    assert proc.returncode != 0
    assert "no hypothesis at" in (proc.stdout + proc.stderr)


# --------------------------------------------------------------------------- #
# what it reports
# --------------------------------------------------------------------------- #
def test_it_counts_the_registry_before_and_after():
    mod = _mod()
    c = mod.counts()
    for key in ("papers", "unread", "survivors", "ranked", "with_body", "distilled"):
        assert key in c, key
    assert c["papers"] >= 284
    assert c["survivors"] <= c["papers"]
    assert c["with_body"] <= c["survivors"]


def test_it_suggests_only_undistilled_high_rated_papers():
    mod = _mod()
    picks = mod.worth_distilling()
    reg = json.loads((REPO_ROOT / "data" / "papers" / "registry.json").read_text())
    for v in picks:
        assert v.get("hypothesis_path") is None, "already distilled"
        assert v.get("feasibility") in ("direct", "adaptable")
        assert v.get("relevance") == "high" or v.get("gate_relevance") == "high"
    # the paper already distilled must not be suggested again
    done = [x["id"] for x in reg["papers"].values() if x.get("hypothesis_path")]
    assert not (set(done) & {v["id"] for v in picks})


def test_an_empty_suggestion_list_does_not_blame_the_data_alone():
    """This message used to say an empty list was a finding about our data: the
    campaign axis had never rated a paper high in 284, because the
    single-instrument constraint removes almost every signal paper.

    **Measured 2026-10-04, that was half right.** The other half was the source.
    arXiv q-fin is mostly method papers: 3 of 284 were directly testable here
    (1.1%), against 22 of 153 from OpenAlex (14.4%). The corpus now holds 28
    directly testable papers where it held 3. An explanation that stops at "our
    data is unusual" is the kind that stops someone looking, so the message now
    names both halves and says to run the ranker before concluding anything."""
    assert "half right" in FLAT
    assert "single-instrument" in FLAT, "the real constraint still belongs in it"
    assert "run the ranker" in FLAT
    # and it no longer presents the old reading as settled
    assert "not a failure" not in SRC


def test_the_dry_run_touches_nothing(tmp_path):
    before = (REPO_ROOT / "data" / "papers" / "registry.json").read_bytes()
    proc = subprocess.run(
        [sys.executable, "scripts/retrieve.py", "--dry-run", "--term", "zzz"],
        capture_output=True, text=True, cwd=str(REPO_ROOT))
    assert proc.returncode == 0
    assert "dry run" in proc.stdout
    assert (REPO_ROOT / "data" / "papers" / "registry.json").read_bytes() == before


def test_one_row_per_claim_in_the_suggestions():
    """The registry keeps every row -- a row carries decisions and a re-fetch
    must not erase them -- but suggesting one paper twice invites two campaigns
    on one idea. Measured 2026-10-04: 40 duplicate titles across 82 rows, which
    is what adding journal and preprint sources to an arXiv-only corpus buys."""
    import re
    mod = _mod()
    picks = mod.worth_distilling()
    keys = [" ".join(re.sub(r"[^a-z0-9 ]+", " ", (v.get("title") or "").lower()).split())
            for v in picks]
    assert len(keys) == len(set(keys)), "the same claim is suggested twice"
    assert "One row per claim" in SRC
