"""Sources beyond arXiv — Brian's point 3, second half.

The corpus was 284 of 284 from arXiv, and `retrieve.py` prints a standing
finding that nothing has ever rated high on the campaign axis in all 284,
"because our single-instrument constraint removes almost every signal paper".
Measured 2026-10-04: that reasoning was half right. arXiv q-fin is mostly method
papers; the empirical intraday-futures literature is in journals. One OpenAlex
query for `intraday futures momentum` returns 58 works whose first hit is
*"Nasdaq-100 Index Futures: Intraday Momentum or Reversal?"* — this project's
question on this project's instrument, which we did not have.

No network here. The live behaviour is recorded in the module docstring and in
`docs/design/paper-retrieval.md`; these pin the parts that must not drift.
"""
from __future__ import annotations

import importlib.util
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (REPO_ROOT / "scripts" / "fetch_scholar.py").read_text()
FLAT = " ".join(SRC.split())


def _mod():
    spec = importlib.util.spec_from_file_location(
        "fetch_scholar", REPO_ROOT / "scripts" / "fetch_scholar.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_row_shape_is_not_reimplemented():
    """One definition of a paper row, and one idempotency guard. A second
    `register()` here would be a second place for "a decision is never
    overwritten by a re-fetch" to be got wrong."""
    assert "fp.register(reg, p, query)" in SRC
    assert "def register" not in SRC
    assert "_fetch_papers_mod" in SRC


def test_openalex_abstracts_are_reconstructed():
    """OpenAlex stores the abstract as {word: [positions]} and gives no plain
    text. Without rebuilding it there is nothing for the screen to read."""
    m = _mod()
    aii = {"momentum": [2], "Intraday": [0], "in": [1], "futures": [3]}
    assert m.abstract_from_index(aii) == "Intraday in momentum futures"
    assert m.abstract_from_index(None) == ""
    assert m.abstract_from_index({}) == ""


def test_ids_are_legal_filenames():
    """`screen_papers.py` reads `data/papers/raw/{id}.json` directly, so an id
    with a slash writes outside the directory. DOIs are full of slashes."""
    m = _mod()
    got = m.safe_id("doi", "10.1016/j.jbankfin.2022.106746")
    assert "/" not in got
    assert got.startswith("doi-")
    assert m.safe_id("openalex", "W4313420244") == "openalex-W4313420244"
    # and nothing escapes upward: no slash, and no `..` either, which is the
    # shape of a traversal even once the slashes are gone
    evil = m.safe_id("doi", "../../etc/passwd")
    assert "/" not in evil and ".." not in evil, evil
    import pathlib as _pl
    base = _pl.Path("/raw")
    assert (base / f"{evil}.json").resolve().parent == base


def test_a_work_with_no_abstract_is_not_registered():
    """The screen reads the abstract. A work without one cannot be screened and
    would sit unread in the registry forever, inflating the corpus count with
    rows nothing can act on."""
    m = _mod()
    import inspect
    sig = inspect.signature(m.main)
    assert "--require-abstract" in SRC
    assert "default=True" in SRC
    assert "cannot be screened" in FLAT


def test_the_unreadable_half_is_counted_not_hidden():
    """Measured: 58 works, 30 with an abstract. "We found 58 and took 30" is a
    different fact from "there were 30", and the run has to say which."""
    assert "skipped (no abstract)" in SRC
    assert "work(s) were found and NOT registered" in SRC


def test_duplicates_across_sources_collapse_on_doi():
    """The same paper arrives from OpenAlex and Crossref. OpenAlex wins: it
    carries the topics, the open-access link and the citation count."""
    assert 'key=lambda x: x["source"] != "openalex"' in SRC
    assert "duplicate(s) across sources dropped" in SRC


def test_already_known_dois_are_not_re_added():
    assert "known_dois" in SRC
    assert "already in the registry" in SRC
    assert "known_titles" in SRC, "title matching too, since 2026-10-04"


def test_one_source_failing_does_not_lose_the_other():
    assert "FAILED: {type(exc).__name__}" in SRC
    assert "continue" in SRC


def test_it_uses_requests_because_of_certificates():
    """Crossref failed with CERTIFICATE_VERIFY_FAILED under urllib: the system
    Python has no usable issuer chain for it. requests ships certifi."""
    assert "import requests" in SRC
    assert "CERTIFICATE_VERIFY_FAILED" in FLAT


def test_it_registers_nothing_and_screens_nothing():
    """Same line every retrieval stage stops at: Amendment 8 makes the commit
    the human act."""
    assert "Nothing was screened or ranked" in SRC
    assert "nq_campaign" not in SRC


def test_titles_dedupe_as_well_as_dois():
    """**DOI dedup is not enough.** Measured after the first real fetch: 40
    duplicate titles across 82 registry rows. Zenodo mints a DOI per version, so
    one paper arrived as `10.5281/zenodo.22892891` and `...892`; an arXiv
    preprint and its journal version are the same claim under two identifiers."""
    m = _mod()
    a = m.title_key("The Distance to the Level Ahead Moves the Time-Outs!")
    b = m.title_key("the distance to the level ahead moves the time outs")
    assert a == b
    assert m.title_key("") == ""
    assert "by_title" in SRC
    assert "by DOI or title" in SRC


def test_the_duplicate_cost_is_stated():
    """Two screens and two rank calls on one paper is the cheap half. The
    expensive half is offering the same claim twice as a question to register."""
    assert "two campaigns on one idea" in FLAT or "counted as two" in FLAT


# --------------------------------------------------------------------------- #
# dedup after the fact, not only at ingest
# --------------------------------------------------------------------------- #
def test_title_dedup_only_ever_guarded_the_ingest_path():
    """`main()` drops a duplicate before registering it, so a pair that entered
    under titles `title_key` could not match -- or entered before any dedup rule
    existed -- stayed live forever with nothing looking again. `--dedup` is the
    pass that looks again."""
    assert "def dedup_registry" in SRC
    assert "--dedup" in SRC


def test_the_body_catches_what_a_title_cannot():
    """**The measured pair.** `openalex-W7110598479` ("Analysis of the Time
    Series Characteristics of Intraday Momentum on the Tokyo Stock Exchange")
    and `openalex-W7208348673` (the same title in Japanese) hold byte-identical
    bodies: same 18,720 chars, same digest, same month, one row per language.
    Neither has a DOI, neither records authors, and their titles share no token,
    so DOI dedup, `title_key` and any author heuristic all miss them."""
    mod = _mod()
    a, b = "openalex-W7110598479", "openalex-W7208348673"
    da, db = mod.body_digest(a), mod.body_digest(b)
    assert da is not None and da == db, "the pair no longer has identical bodies"
    assert mod.title_key("Analysis of the Time Series Characteristics") != \
        mod.title_key("東証日内モーメンタムの時系列的特徴に関する考察")
    assert mod.body_digest("no-such-paper-id") is None


def test_the_corpus_records_that_pair_as_a_duplicate():
    import json
    mod = _mod()
    reg = json.loads((REPO_ROOT / "data" / "papers" / "registry.json").read_text())
    rows = reg["papers"]
    marked = [r for r in rows.values() if r.get("duplicate_reason") == "identical body"]
    assert marked, "the body-digest rule has never been applied to the corpus"
    for r in marked:
        canon = rows[r["duplicate_of"]]
        assert not canon.get("duplicate_of"), "a duplicate points at a duplicate"


def test_the_canonical_row_never_orphans_a_decision():
    """A row some campaign or hypothesis already points at must survive, because
    marking it a duplicate would orphan that reference."""
    mod = _mod()
    used = {"id": "b", "campaign_ids": ["nq-s07-02"]}
    rich = {"id": "a", "doi": "10.1/x", "published": "2020-01-01"}
    assert mod._canonical([rich, used]) is used
    hyp = {"id": "c", "hypothesis_path": "config/hypotheses/x.md"}
    assert mod._canonical([rich, hyp]) is hyp


def test_the_canonical_order_is_completeness_then_readability():
    mod = _mod()
    doi = {"id": "b", "doi": "10.1/x"}
    dated = {"id": "a", "published": "2020-01-01"}
    assert mod._canonical([dated, doi]) is doi, "a DOI should outrank a date"
    jp = {"id": "a", "published": "2023-11-01", "title": "東証日内モーメンタム"}
    en = {"id": "b", "published": "2023-11-01", "title": "Intraday Momentum"}
    assert mod._canonical([jp, en]) is en, (
        "between rows equal on metadata, keep the title a reader can recognise")
    assert mod._mostly_ascii("Intraday Momentum")
    assert not mod._mostly_ascii("東証日内モーメンタムの時系列的特徴に関する考察")


def test_a_dropped_title_is_carried_across_and_not_lost():
    """The canonical is chosen on metadata completeness, which can leave the live
    row holding a title nobody here reads."""
    import json
    reg = json.loads((REPO_ROOT / "data" / "papers" / "registry.json").read_text())
    canon = reg["papers"]["openalex-W7208348673"]
    assert any("Intraday Momentum" in t for t in (canon.get("also_titled") or [])), (
        "the English title was dropped rather than carried across")


def test_dedup_marks_and_never_deletes():
    body = SRC.split("def dedup_registry", 1)[1].split("\ndef ", 1)[0]
    for forbidden in ("del reg[", ".pop(", "remove("):
        assert forbidden not in body, f"{forbidden} removes a row"
    assert "duplicate_of" in body


def test_every_existing_mark_is_re_derivable_by_the_committed_rules():
    """The 43 rows marked before this pass existed were marked by hand. If the
    committed rules cannot reproduce them, the registry holds judgements nobody
    can re-derive."""
    import json
    mod = _mod()
    rows = json.loads(
        (REPO_ROOT / "data" / "papers" / "registry.json").read_text())["papers"]
    marked = [r for r in rows.values() if r.get("duplicate_of")]
    assert len(marked) >= 43
    orphans = []
    for r in marked:
        canon = rows.get(r["duplicate_of"])
        if canon is None:
            orphans.append(r["id"]); continue
        same_body = (mod.body_digest(r["id"]) is not None
                     and mod.body_digest(r["id"]) == mod.body_digest(canon["id"]))
        same_doi = bool(r.get("doi")) and \
            (r.get("doi") or "").lower() == (canon.get("doi") or "").lower()
        same_title = mod.title_key(r.get("title", "")) == \
            mod.title_key(canon.get("title", ""))
        if not (same_body or same_doi or same_title):
            orphans.append(r["id"])
    assert not orphans, f"not re-derivable: {orphans}"


def test_resolve_oa_closes_a_hardcoded_none_and_sends_no_new_address():
    """The Crossref branch sets `oa_url: None` unconditionally, so every Crossref
    row entered with no address for its body. `--resolve-oa` asks OpenAlex about
    a DOI we already hold, using an API this pipeline already calls."""
    assert "def oa_for_doi" in SRC and "--resolve-oa" in SRC
    body = SRC.split("def oa_for_doi", 1)[1].split("\ndef ", 1)[0]
    assert "OPENALEX" in body, "it is not using the API this pipeline already calls"
    assert "mailto" not in body.lower(), (
        "a contact address is being added to this request that was not there")
