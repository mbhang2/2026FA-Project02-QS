"""Amendment 12 — trials in generations, not a chain.

Signed off 2026-10-04. The first amendment here that trades statistical power
for wall clock; the eleven before it all tightened something.

**What makes it safe is that the correction is policy-independent.**
`max_of_n` takes each scored candidate's own null sample and simulates the
best-of-N. It corrects for how many candidates were scored, never for how they
were chosen. A hundred candidates in twenty-five generations of four are a
hundred looks, counted identically to a hundred proposed one at a time.

Proven on `a12-gen-w4b`: 8 trials, two generations of 4, **8 minutes against
~30 sequential**, 8 distinct strategy digests (no collision), 8 distinct
artifact fingerprints, every source present, and exactly 8 rows added to
`global_n()`.
"""
from __future__ import annotations

import json
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (REPO_ROOT / "scripts" / "nq_campaign.py").read_text()
# Comment markers stripped before flattening: most of the reasoning here lives
# in hard-wrapped comments, and joining the raw lines leaves a stray '#' in the
# middle of every sentence.
import re as _re
FLAT = " ".join(_re.sub(r"^\s*#\s?", "", SRC, flags=_re.M).split())


def test_amendment_12_is_in_force():
    import sys
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import prereg
    assert prereg.in_force(12)
    assert "2026-10-04" in prereg.status(12)


def test_the_amendment_states_the_trade_it_makes():
    """Power for wall clock. Every other amendment tightened something, so the
    direction of this one has to be impossible to miss."""
    doc = (REPO_ROOT / "docs" / "design" / "pre-registration.md").read_text()
    a12 = doc[doc.index("## Amendment 12"):]
    flat = " ".join(a12.split())
    assert "trades statistical power for wall clock" in flat
    assert "over-corrects" in flat, "the direction of the bias must be stated"
    assert "safe" in flat, "over-correcting is the safe direction; say so"
    # and it must not claim parallelism fixes multiplicity
    assert "does not launder" in flat


def test_the_amendment_refuses_beam_search():
    """Keeping the best k of a generation and branching only from those is a
    different change and needs its own pre-registration."""
    doc = (REPO_ROOT / "docs" / "design" / "pre-registration.md").read_text()
    a12 = doc[doc.index("## Amendment 12"):]
    flat = " ".join(a12.split())
    assert "Selection between generations" in flat
    assert "winners and losers alike" in flat


def test_width_one_is_the_old_procedure():
    """`None` and `1` both mean the chain the first eighteen campaigns ran, and
    at W = 1 the trial runs in THIS repository, not in a worker -- so nothing
    about a W = 1 campaign changes."""
    from src.core.loop.campaign import CampaignRecord
    f = CampaignRecord.__dataclass_fields__["generation_width"]
    assert f.default is None, (
        "a default of 1 would move the digest of every record committed before "
        "this field existed")
    assert "width = max(1, int(record.generation_width or 1))" in SRC
    assert "if width == 1:" in SRC


def test_the_record_digest_is_unmoved_by_the_new_field():
    """`digest()` drops None keys. A record committed before Amendment 12 must
    hash to what it always did, or `CampaignRecord.read` raises
    `CampaignTampered` on records nobody touched."""
    from src.core.loop.campaign import CampaignRecord
    for name in ("nq-s04-01", "nq-s03-01", "nq-t72b-01"):
        p = REPO_ROOT / "runs" / "campaigns" / name / "campaign.json"
        if p.exists():
            CampaignRecord.read(p)      # raises if the digest moved


def test_one_history_per_generation_snapshotted_before_it_runs():
    """The sentence Amendment 12 amended: everyone in a generation sees every
    trial completed before the generation began, and nothing from its own."""
    assert "shown = list(rows)" in SRC
    assert "snapshotted before any of it" in FLAT


def test_results_are_folded_in_index_order():
    """The champion advances on a strict improvement, so the order results are
    merged decides which of two equal-scoring candidates becomes the base.
    Thread completion order is arbitrary; trial index is not."""
    assert "sorted(results, key=lambda r: r[0])" in SRC
    assert "Thread completion order is arbitrary" in FLAT


def test_the_generation_is_stamped_before_the_row_is_written():
    """The first version stamped the in-memory copy after appending, so the
    file recorded null for all eight trials of the proof run -- the amendment's
    own disclosure requirement unmet by the code implementing it."""
    i = SRC.index('row["generation"] = gen_start')
    j = SRC.index("with ledger.open(\"a\") as fh:")
    assert i < j, "stamp the row, then write it"


def test_worker_sources_are_merged_back():
    """`run.py` stores a trial's source beside the ledger it wrote, which for a
    parallel trial is the worker's. The campaign row names the digest and a
    reader follows it from the campaign directory."""
    assert "def _merge(" in SRC
    assert "points at nothing" in FLAT


def test_the_proof_run_is_on_disk_and_correct():
    d = REPO_ROOT / "runs" / "campaigns" / "a12-gen-w4b"
    if not (d / "ledger.jsonl").exists():
        return
    rows = [json.loads(x) for x in (d / "ledger.jsonl").read_text().splitlines()
            if x.strip()]
    assert len(rows) == 8
    assert [r["campaign_index"] for r in rows] == list(range(8))
    assert [r["generation"] for r in rows] == [0, 0, 0, 0, 1, 1, 1, 1]
    assert all(r["generation_width"] == 4 for r in rows)
    digests = [r["strategy_digest"] for r in rows]
    assert len(set(digests)) == 8, "two trials shared a strategy -- a collision"
    assert len({r["artifact_fingerprint"] for r in rows}) == 8
    for r in rows:
        assert (d / "sources" / pathlib.Path(r["strategy_source"]).name).exists()


def test_a_worker_ledger_is_not_on_a_counted_path():
    """Worker copies are scratch. If `ledger_paths` reached them, every parallel
    trial would be counted twice in N."""
    from src.core.runlog import ledger_paths
    paths = [str(p) for p in ledger_paths()]
    assert not any(".scratch" in p for p in paths)


# --------------------------------------------------------------------------- #
# two traps closed on the way (2026-10-04)
# --------------------------------------------------------------------------- #
def test_the_default_proposer_is_not_haiku():
    """docs/setup/MODEL-ACCESS.md: `claude-haiku-4-5` passes a small prompt and
    is substituted to `gemini-3.1-pro-preview` at our real ~167,000-character
    proposal prompt. The register preflight passes because the preflight prompt
    is small; the first real proposal then dies. The first a12 proof
    registration was lost to exactly this."""
    import re
    m = re.search(r'^MODEL = "([^"]+)"', SRC, re.M)
    assert m and m.group(1) != "claude-haiku-4-5"


def test_no_probe_reaches_the_record():
    """`True if a.probe else None` collapsed False into None, so `--no-probe`
    silently took the config default. The same bug the flag's own help text
    describes being fixed once already, reintroduced at the call site."""
    assert "a.probe, a.generation_width)" in SRC
    assert "True if a.probe else None" not in SRC


def test_a_plan_can_actually_set_the_generation_width():
    """**The flag was added to the driver's argv loop and not to the dict that
    loop reads.** `generation_width: 4` in session-05's plan reached nothing,
    the first campaign registered as a chain, and the 19-hour path the amendment
    was signed to avoid is what started running. Two trials were spent before it
    was caught.

    A flag the driver can pass and a plan cannot set is a flag that silently
    does nothing, so both halves are asserted here.
    """
    src = (REPO_ROOT / "scripts" / "research_session.py").read_text()
    # the plan key reaches the per-campaign entry ...
    assert '"generation_width": fam.get("generation_width")' in src
    # ... and the entry reaches the register argv
    assert '("--generation-width", "generation_width")' in src
    i = src.index('"generation_width": fam.get')
    j = src.index('("--generation-width", "generation_width")')
    assert i < j, "the entry is built before the argv is assembled"


def test_the_committed_session_plan_asks_for_generations():
    import yaml
    p = REPO_ROOT / "config" / "sessions" / "session-05b-opening-reversal.yaml"
    if not p.exists():
        return
    fam = yaml.safe_load(p.read_text())["family"]
    assert fam["generation_width"] == 4
    assert fam["n"] == 100


def test_every_artefact_beside_a_worker_ledger_is_merged():
    """**`runlog.py` writes three directories beside a ledger** --
    `sources/`, `weights/` and `fills/` -- and the first version of the merge
    copied `sources/` only. The missing one was `fills/`, which the sign-flip
    null reconstructs round turns from, so `stage_nulls` found nothing for any
    candidate, wrote 100 empty samples, and the gate reported `n_nulls = 0` and
    `p = 1.0` on a campaign with 45 finite scores.

    Asserted as "copy whatever is there" rather than against the three names,
    because enumerating names is exactly what broke.
    """
    assert "for src_dir in sorted(wledger.parent.iterdir()):" in SRC
    assert "Every sibling directory, not a list of names" in FLAT
    # and runlog really does write more than one
    runlog = (REPO_ROOT / "src" / "core" / "runlog.py").read_text()
    for name in ("sources", "weights", "fills"):
        assert f'"{name}"' in runlog, name


def test_the_feedback_edge_reports_keys_that_exist():
    """It printed `v['conclusion']`, which `verdicts_for()` has never returned.
    The KeyError was raised AFTER the registry write, so the edge did its job
    and then reported "feedback edge FAILED" -- the opposite of the truth, on
    nq-s05b-01 and on every campaign seeded by a paper before it. A best-effort
    step that lies about succeeding is worse than one that stays quiet."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "record_outcome", REPO_ROOT / "scripts" / "record_outcome.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    v = mod.verdicts_for("nq-s05b-01")
    if v is None:
        return
    assert "conclusion" not in v
    src = (REPO_ROOT / "scripts" / "nq_campaign.py").read_text()
    assert "v['conclusion']" not in src
    for key in ("gate", "committee", "vetoed_by"):
        assert key in v, key
        assert f"v['{key}']" in src or f'v.get("{key}")' in src, key


def test_a_truncated_family_cannot_spend_its_validation_looks():
    """**The loss that motivated this.** A `--resume` skipped nq-s05b-01 as
    already run, which also skipped the refinement that writes the NEXT
    question -- so campaigns 2 and 3 had no question, were passed over, and the
    session fell straight through to phase 2 and validated a family of one. The
    L2 then ran at M = 1 over a declared M of 3.

    Validation is one look per campaign and cannot be taken back. Nothing
    downstream could detect the truncation, because the L2 reports the M it was
    given, so it has to be refused where the difference is still visible.

    A human Ctrl-C is a legitimate stop and still validates what ran; this
    guard fires only when campaigns were passed over without being asked for.
    """
    src = (REPO_ROOT / "scripts" / "research_session.py").read_text()
    assert "phase 2 REFUSED: the declared family is incomplete" in src
    assert "not stopped_by_cue" in src, "a human cue must still validate"
    i = src.index("phase 2 REFUSED")
    j = src.index('print("\\nphase 2: validation at the stop')
    assert i < j, "the guard must run before any validation"


def test_resume_skipping_a_campaign_is_recorded_as_such():
    src = (REPO_ROOT / "scripts" / "research_session.py").read_text()
    assert 'log["campaigns"][name] = "skipped (resume)"' in src
