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


def test_a_resumed_campaign_still_produces_the_next_question():
    """**The other half of the 2026-10-04 loss.** The refinement lived inside
    the stage loop's `else:`, so a `--resume` that skipped a completed campaign
    skipped its refinement too; nq-s05b-02 and -03 then had no question, were
    passed over, and the session validated one campaign of a declared three.

    One definition, two callers -- and the resume path takes the campaign's own
    question from its record rather than from whatever the loop was carrying,
    because resuming at campaign 2 must refine from campaign 2's question and
    not from the seed.
    """
    src = (REPO_ROOT / "scripts" / "research_session.py").read_text()
    assert "def _refine_after(" in src
    assert src.count("_refine_after(") == 3, "one definition, two call sites"
    skip = src[src.index('log["campaigns"][name] = "skipped (resume)"'):]
    skip = skip[:skip.index("        try:")]
    assert "_refine_after(" in skip, "the resume path must refine"
    assert "hypothesis_path" in skip, "and from the campaign's OWN question"


def test_refining_after_a_stop_is_refused_rather_than_improvised():
    """`refine_hypothesis.py` refuses when a validation artefact is readable
    (§4: the information must not be in the room). A resume AFTER the stop
    therefore cannot continue the family, and must abort rather than carry on
    -- which is what makes putting validation at the stop meaningful."""
    import subprocess
    import sys
    d = REPO_ROOT / "runs" / "campaigns" / "nq-s05b-01"
    if not (d / "validation.json").exists():
        return
    proc = subprocess.run(
        [sys.executable, "scripts/refine_hypothesis.py", "--campaign",
         "nq-s05b-01", "--dry-run"],
        capture_output=True, text=True, cwd=str(REPO_ROOT))
    assert "REFUSED" in (proc.stdout + proc.stderr)
    assert "validation" in (proc.stdout + proc.stderr).lower()


# --------------------------------------------------------------------------- #
# only a completed campaign is validated (2026-10-05)
# --------------------------------------------------------------------------- #
def test_validation_is_only_for_campaigns_that_completed():
    """**The look nq-s06-03 should never have had.** It died at trial 36 of 100
    on a gateway `ModelSubstituted`, so `nulls` and `report` never ran and it had
    no train-side gate at all -- its champion was picked by the carry rule alone,
    never corrected, never audited. It was validated anyway, and its +0.1371
    became the L2's observed best over two campaigns that had completed 100
    trials each.

    The line was `names = completed if stopped_by_cue else names`: a human stop
    validated what ran, and every other ending validated the whole declared
    family whether it ran or not. The refinement-failure path learned this on
    2026-09-30; the run-failure path never did.
    """
    src = (REPO_ROOT / "scripts" / "research_session.py").read_text()
    assert "names = completed\n" in src
    assert "names = completed if stopped_by_cue else names" not in src
    assert "excluded = [n for n in names if n not in completed]" in src


def test_the_phase_2_guard_keys_on_completion_not_on_having_a_question():
    """My first version of this guard only caught campaigns with no question,
    which is why it missed nq-s06-03 -- that one had a question, registered
    fine, and failed mid-run. A guard narrower than the rule it protects is a
    guard that will be surprised again."""
    src = (REPO_ROOT / "scripts" / "research_session.py").read_text()
    assert "missing = excluded if not stopped_by_cue else []" in src
    assert '"no question" in str(log["campaigns"]' not in src
    # a human cue is still a decision and still validates what ran
    i = src.index("missing = excluded")
    assert "stopped_by_cue" in src[i:i + 120]


def test_a_truncated_campaign_had_no_gate_which_is_the_point():
    """Stated against the artefacts: nq-s06-03 has a validation.json and no
    tier2-summary.json. That combination should be impossible and is the
    signature of the defect."""
    d = REPO_ROOT / "runs" / "campaigns" / "nq-s06-03"
    if not d.exists():
        return
    assert (d / "validation.json").exists(), "it was validated"
    assert not (d / "tier2-summary.json").exists(), (
        "it never had a train gate -- if this file now exists the campaign was "
        "completed later and this test should be retired")


# --------------------------------------------------------------------------- #
# Amendment 7 reaches every agent (2026-10-05)
# --------------------------------------------------------------------------- #
def test_the_probe_is_armed_on_every_agent_not_just_the_first():
    """**Three of every four proposals skipped Amendment 7.** `stage_run` armed
    the probe on `agent` and Amendment 12 then built the other `width - 1` with
    `agent_for(...)`, which arms nothing. Measured: 41 probe rows for 100 trials
    in nq-s06-01, 42 for 100 in nq-s06-02 -- and nq-s06-02 then crashed 35 of
    100 proposals on two API-misuse shapes the probe catches in one synthetic
    run, because three quarters of them never got the chance to repair."""
    assert "def _arm_probe(a) -> None:" in SRC
    assert SRC.count("_arm_probe(") == 3, "one definition, two call sites"
    # the extra agents are armed where they are built
    i = SRC.index("for _ in range(width - 1):")
    assert "_arm_probe(extra)" in SRC[i:i + 400]


def test_probe_coverage_is_measured_against_trials_not_asserted_as_a_flag():
    """`probe_before_trial: true` was in every affected campaign's record. A test
    that asserts the flag passes while the probe reaches a quarter of proposals,
    which is what happened -- so coverage is counted instead.

    Campaigns run BEFORE the fix are expected to fail this and are named, rather
    than the threshold being lowered until they pass.
    """
    import json
    broken = {"nq-s05b-01", "nq-s06-01", "nq-s06-02", "nq-s06-03"}
    camps = REPO_ROOT / "runs" / "campaigns"
    for d in sorted(camps.glob("*/probe-ledger.jsonl")):
        name = d.parent.name
        rec = d.parent / "campaign.json"
        if not rec.is_file():
            continue
        r = (json.loads(rec.read_text()).get("record") or {})
        if not r.get("probe_before_trial") or (r.get("generation_width") or 1) < 2:
            continue
        led = d.parent / "ledger.jsonl"
        trials = len([x for x in led.read_text().splitlines() if x.strip()])
        rows = len([x for x in d.read_text().splitlines() if x.strip()])
        if name in broken:
            assert rows < trials, (
                f"{name} was run before the probe fix and should show the gap; "
                f"if it no longer does, retire it from `broken`")
        else:
            assert rows >= trials, (
                f"{name}: {rows} probe rows for {trials} trials -- Amendment 7 "
                f"reached only {rows / trials:.0%} of proposals")


def test_the_crash_analysis_records_what_is_not_fixed():
    """Annotating the strategy template would turn both crash shapes into static
    errors, but `config/prompts/strategy-agent.md` is in ARTIFACT_GLOBS, so it
    moves the artifact fingerprint of every later campaign. Left as a decision,
    and said so."""
    f = REPO_ROOT / "docs" / "reviews" / "crash-analysis" / "README.md"
    flat = " ".join(f.read_text().split())
    assert "mypy can only check what is typed" in flat
    assert "ARTIFACT_GLOBS" in flat
    assert "not separable from this campaign alone" in flat


# --------------------------------------------------------------------------- #
# crash prevention and mid-campaign recovery (2026-10-05)
# --------------------------------------------------------------------------- #
def test_closed_bars_supports_get():
    """**13 trials died on `ctx.bars.get("5m")`.** The object answers "did my bar
    close just now" and supports `__getitem__`, `__contains__`, `__len__` and
    `__iter__`, so a model reads it as a mapping and reaches for the one mapping
    method it did not have. Every one of those proposals was right about what it
    wanted and died on the spelling.

    Both copies: the in-process context and the remote child carry their own.
    """
    import sys
    sys.path.insert(0, str(REPO_ROOT))
    from src.core.engine.context import _ClosedBars
    assert hasattr(_ClosedBars, "get")
    child = (REPO_ROOT / "src" / "core" / "engine" / "remote" / "child.py").read_text()
    assert "def get(self, sub" in child, "the remote child needs it too"

    class _Ctx:
        def closed_ids(self):
            return ()
    b = _ClosedBars.__new__(_ClosedBars)
    b._ctx = _Ctx()
    assert b.get("5m") is None
    assert b.get("5m", "d") == "d"


def test_window_errors_do_not_advise_the_impossible():
    """**18 trials died here, several retrying the advice.** The message said
    "Declare a longer window", and a strategy CANNOT: the window is fixed by the
    committed profile and `run.py` takes no arguments. An error that tells you to
    do something impossible is worse than one that only states the fact."""
    import re
    child = (REPO_ROOT / "src" / "core" / "engine" / "remote" / "child.py").read_text()
    # Adjacent f-string literals are joined before matching: flattening alone
    # leaves the `" f"` between them in the middle of every wrapped sentence,
    # which is the trap that made the first version of this test fail.
    flat = " ".join(re.sub(r'"\s*f?"', "", child).split())
    assert "Declare a longer window" not in flat
    assert flat.count("fixed by the committed profile and a strategy CANNOT") == 3, (
        "all three guards -- index, iteration and last(k) -- must say it. The "
        "first version of this test asserted 2 and found the third site.")
    # and both offer a remedy that exists
    assert "accumulate on `self`" in flat
    assert "keep what you need on `self` as it arrives" in flat
    assert "ctx.history('5m')[-20:]" in flat, "name a slice that is inside it"


def test_a_partial_campaign_is_resumable_not_finished():
    """`already_run` was "ledger.jsonl exists", so nq-s06-03 -- dead at trial 36
    of 100 -- would have been skipped by a resume as "already run" and lost. It
    was recoverable the whole time: `--stage run` resumes at `len(rows)`."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_rs2", REPO_ROOT / "scripts" / "research_session.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.already_run("nq-s06-01") is True, "100/100 is finished"
    if (REPO_ROOT / "runs" / "campaigns" / "nq-s06-03" / "campaign.json").exists():
        assert m.trials_run("nq-s06-03") < m.budget_of("nq-s06-03")
        assert m.already_run("nq-s06-03") is False, (
            "36 of 100 trials is not a campaign that ran")
