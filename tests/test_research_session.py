"""One research session: K campaigns, validation at the stop, then L2.

No network, no campaigns run. What is asserted is the ORDER and the refusals,
because the order is the protocol.

The load-bearing property is phase separation. §4:

> At the stop, all M winners are scored on validation *then*, at once. This
> removes the temptation rather than relying on discipline ... a human who reads a
> committee report that contains a validation number has seen it, whatever they
> intended.

Every campaign so far was validated immediately after its own report, which is not
that. This driver runs register/run/nulls/report for all campaigns first and
validates afterwards, so a human deciding whether to run campaign k+1 has not seen
a validation number because none exists yet.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (REPO_ROOT / "scripts" / "research_session.py").read_text()
# Prose assertions run against this: the source is hard-wrapped, so a phrase
# the test cares about can be split across a line break.
FLAT = " ".join(SRC.split())


def _mod():
    spec = importlib.util.spec_from_file_location(
        "research_session", REPO_ROOT / "scripts" / "research_session.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _plan(tmp_path, names=("a", "b"), session="s1"):
    import yaml
    p = tmp_path / "plan.yaml"
    p.write_text(yaml.safe_dump({
        "session": session,
        "campaigns": [{"name": n, "hypothesis": f"config/hypotheses/{n}.md"}
                      for n in names]}))
    return p


# --------------------------------------------------------------------------- #
# the order IS the protocol
# --------------------------------------------------------------------------- #
def test_validation_comes_after_every_campaign_not_after_each_one():
    """Phase 1 is register/run/nulls/report for all of them. Phase 2 is
    validation. A `validate` inside the phase-1 loop would defeat §4's whole
    defence against optional stopping."""
    phase1 = SRC.index("phase 1: campaigns")
    phase2 = SRC.index("phase 2: validation at the stop")
    phase3 = SRC.index("phase 3: the L2 gate")
    phase4 = SRC.index("phase 4: the holdout")
    assert phase1 < phase2 < phase3 < phase4, "the phases are ordered"

    body1 = SRC[phase1:phase2]
    assert '"validate"' not in body1, (
        "a validate call inside phase 1 would show a human a validation number "
        "before they decide whether to run the next campaign")
    for stage in ("run", "nulls", "report"):
        assert f'"{stage}"' in body1, stage
    assert '"validate"' in SRC[phase2:phase3]


def test_the_driver_says_why_the_order_matters():
    assert "validation runs AT THE STOP" in FLAT
    assert "temptation rather than relying on discipline" in FLAT
    assert "only exists if the numbers do not exist yet" in FLAT


def test_a_report_without_validation_is_an_intended_consequence():
    """A committee that never saw a validation number judges something different.
    That has to be stated, not discovered later as a bug."""
    assert "committee that never saw a validation number" in FLAT
    assert "intended consequence, not a side effect" in FLAT


# --------------------------------------------------------------------------- #
# what it refuses
# --------------------------------------------------------------------------- #
def test_the_holdout_is_never_taken_unless_it_was_armed_at_launch():
    """This test used to assert the driver could never pass `--reason` at all.
    That changed deliberately on 2026-09-30, at Michael's request, so an
    unattended session can end at the holdout.

    What it must still guarantee is the reason the old assertion existed: a
    driver that spends the look as the last line of every overnight script will
    spend it by accident on the first one. Five things stand between the flag and
    the data, and all five are asserted here.
    """
    phase4 = SRC[SRC.index("phase 4: the holdout"):]

    # 1. default is check-only
    assert '"--check"' in phase4
    assert "checked only; not armed" in phase4
    # 2. --reason is reachable ONLY under the armed branch
    assert "if a.holdout_if_l2_passes:" in phase4
    armed = phase4[phase4.index("if a.holdout_if_l2_passes:"):]
    assert '"--reason", a.holdout_if_l2_passes' in armed
    assert '"--reason"' not in phase4[:phase4.index("if a.holdout_if_l2_passes:")]
    # 3. the check must clear first, inside the armed branch
    assert "if code != 0:" in armed
    assert "armed; refused by holdout.py --check" in armed
    # 4. the flag carries a reason, so the record says why
    assert "holdout_reason" in phase4
    # 5. arming happens at launch and the file says why that matters
    assert "one look, EVER -- not one per session" in FLAT
    assert "before any validation number exists" in FLAT


def test_arming_cannot_be_decided_after_seeing_a_validation_number():
    """The flag is parsed with the rest of argv, before phase 1 runs. If it could
    be supplied later -- a prompt, an env var read at phase 4 -- the decision to
    spend the look could be made in response to a validation result, which is the
    one thing §4 forbids outright."""
    parse = SRC.index('ap.add_argument("--holdout-if-l2-passes"')
    phase2 = SRC.index("phase 2: validation at the stop")
    assert parse < phase2, "the flag is read at launch, before any number exists"
    assert "input(" not in SRC, "no interactive prompt may arm it mid-session"
    # **Narrowed 2026-10-05, and narrowed rather than dropped.** This banned the
    # string `environ` outright, which is the right instinct for the holdout and
    # which caught Amendment 13's `PRACTICUM_RUNS_ROOT` immediately. That read
    # cannot arm anything -- it happens at module import, before argv is parsed,
    # and it sets only where output goes; `holdout.py`'s RESULT is pinned to the
    # real `runs/` precisely so a redirected root cannot find "no prior look".
    #
    # So the rule becomes: the ONE permitted environment read is the output
    # root, and it is read before the parser exists. A second one, or that one
    # moved later, fails here.
    import re
    reads = [m.start() for m in re.finditer(r"environ", SRC)]
    assert len(reads) == 1, (
        f"{len(reads)} environment reads; exactly one is permitted and it is "
        f"the Amendment 13 output root")
    assert "PRACTICUM_RUNS_ROOT" in SRC[reads[0] - 80:reads[0] + 80]
    assert reads[0] < parse, (
        "the output root is read at import, before argv is parsed -- an "
        "environment read AFTER the parser could be set in response to a "
        "validation number")


def test_the_family_is_the_plan_and_the_plan_is_written_first():
    """Dropping the campaigns that did badly shrinks M and inflates the maximum's
    significance. The family is every campaign named in the plan, and the plan is
    copied into the session record."""
    assert '"declared_family": names' in SRC
    assert '"--campaigns", ",".join(names)' in SRC
    assert "written before the session" in FLAT.lower()


def test_it_does_not_invent_questions():
    """Amendment 15: a session may run a statement a human chose, written into
    a hypothesis at registration -- but choosing WHICH statements is never the
    session's, and it never distils a paper into a question of its own."""
    assert "will not invent questions" in FLAT
    assert "Amendment 15" in FLAT
    assert "never the session's" in FLAT
    # it never calls the distiller
    assert "distil_paper" not in SRC


def test_duplicate_campaign_names_are_refused():
    assert "duplicate campaign names in the plan" in FLAT


# --------------------------------------------------------------------------- #
# the plan, and resuming
# --------------------------------------------------------------------------- #
def test_the_dry_run_reports_the_family_and_touches_nothing(tmp_path):
    plan = _plan(tmp_path, names=("nq-zzz-01", "nq-zzz-02"))
    proc = subprocess.run(
        [sys.executable, "scripts/research_session.py", "--plan", str(plan),
         "--dry-run"], capture_output=True, text=True, cwd=str(REPO_ROOT))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "validation runs AT THE STOP" in proc.stdout
    assert "the L2 family would be: nq-zzz-01, nq-zzz-02" in proc.stdout
    assert "nothing was run" in proc.stdout
    assert not (REPO_ROOT / "runs" / "campaigns" / "nq-zzz-01").exists()
    assert not (REPO_ROOT / "runs" / "sessions" / "s1.json").exists()


def test_an_empty_or_missing_plan_is_refused(tmp_path):
    import yaml
    for content, expected in (
            (None, "no plan at"),
            (yaml.safe_dump({"session": "x", "campaigns": []}),
             "names no campaigns")):
        p = tmp_path / "p.yaml"
        if content is None:
            p = tmp_path / "absent.yaml"
        else:
            p.write_text(content)
        proc = subprocess.run(
            [sys.executable, "scripts/research_session.py", "--plan", str(p)],
            capture_output=True, text=True, cwd=str(REPO_ROOT))
        assert proc.returncode != 0
        assert expected in (proc.stdout + proc.stderr), expected


def test_resume_skips_only_what_already_has_a_ledger():
    mod = _mod()
    assert mod.already_run("nq-ml-06") is True, "a real campaign with a ledger"
    assert mod.already_run("nq-does-not-exist") is False
    assert mod.registered("nq-ml-06") is True
    assert "a machine that died in hour six does not cost hours one to five" in FLAT


def test_the_example_plan_names_only_committed_hypotheses():
    """A plan pointing at an uncommitted file would register a campaign on a
    question that is not a pre-registration."""
    import yaml
    plan = yaml.safe_load(
        (REPO_ROOT / "config" / "sessions" / "example-session.yaml").read_text())
    for c in plan["campaigns"]:
        h = REPO_ROOT / c["hypothesis"]
        assert h.exists(), c["hypothesis"]
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch",
                                  c["hypothesis"]],
                                 capture_output=True, cwd=str(REPO_ROOT))
        assert tracked.returncode == 0, f"{c['hypothesis']} is not committed"


def test_the_l2_verdict_is_printed_and_not_left_in_a_file():
    """Four times in this project a number has been computed correctly and never
    reached its consumer. `_run` prints one line; the gate's rule (a)/(b) split,
    each campaign's distance above its own null, and the Task 70 disagreement
    warning are not on it."""
    phase3 = SRC[SRC.index("phase 3: the L2 gate"):SRC.index("phase 4: the holdout")]
    assert "L2 verdict" in phase3
    assert "rule (a)" in phase3 and "rule (b)" in phase3
    assert "sds_above_own_null" in phase3
    assert "THE TWO L2 STATISTICS DISAGREE" in phase3
    assert "Amendment 10" in phase3
    assert "pre-registration decision, not this script's" in FLAT


# --------------------------------------------------------------------------- #
# Ctrl-C is the stop signal
# --------------------------------------------------------------------------- #
def test_the_first_cue_goes_to_the_stop_and_the_second_aborts():
    """§4 lets a human stop for any reason that is not a validation result. Ctrl-C
    is the honest version of that: at the moment it arrives, phase 2 has not run,
    so no validation number exists and the stop provably cannot be a response to
    one."""
    assert "except KeyboardInterrupt:" in SRC
    assert "stopped_by_cue = True" in SRC
    assert "ABORTED on a second cue" in SRC
    assert "provably cannot be stopping" in FLAT or \
           "cannot be stopping because of one" in FLAT
    # the first cue is caught INSIDE phase 1, not around the whole session
    p1 = SRC.index("phase 1: campaigns")
    p2 = SRC.index("phase 2: validation at the stop")
    assert "except KeyboardInterrupt:" in SRC[p1:p2]


def test_an_interrupted_campaign_is_recorded_but_not_in_the_family():
    """The family is the campaigns that reached `report`. An interrupted one has
    no winner to contribute, and saying so is not family-shrinking -- there is
    nothing of its to drop."""
    assert "interrupted mid-flight (human cue)" in SRC
    assert '"declared_family"] = completed' in SRC
    assert "their trials count, they contribute no candidate" in FLAT


def test_the_holdout_must_be_armed_at_launch_or_it_is_only_checked():
    """§8 gives 73 sessions one look EVER, not one per session. The flag exists
    only at launch because that is the one moment the decision can be made with no
    validation number in front of anyone."""
    assert '"--holdout-if-l2-passes"' in SRC
    assert "checked only; not armed" in SRC
    assert "one look, EVER -- not one per session" in FLAT
    assert "before any validation" in FLAT
    # and it never bypasses holdout.py's own refusals
    p4 = SRC[SRC.index("phase 4: the holdout"):]
    assert '"--check"' in p4
    assert "armed; refused by holdout.py --check" in p4


# --------------------------------------------------------------------------- #
# what session-01 taught, the hard way
# --------------------------------------------------------------------------- #
def test_a_stage_never_swallows_its_own_failure():
    """session-01 printed three campaigns at `exit 1` with the reason nowhere: a
    transient model substitution, captured in a traceback and dropped. A summary
    that replaces the evidence instead of pointing at it is the defect."""
    assert "LOGS.mkdir(parents=True, exist_ok=True)" in SRC
    assert "f.write_text(body)" in SRC
    assert 'print(f"        | {line}"[:150]' in SRC
    assert "could as easily have been eight hours" in FLAT


def test_a_transient_endpoint_fault_is_retried_and_the_guard_is_not_softened():
    """`--stage run` resumes, so retrying costs nothing already done. What must
    NOT happen is the guard being relaxed: ModelSubstituted exists because
    continuing would write a record naming a model that did not answer."""
    assert "ModelSubstituted" in SRC
    assert "MAX_STAGE_RETRIES" in SRC
    assert "must NOT be softened" in FLAT
    assert "not to believe it" in FLAT
    # only `run` is retried -- nulls and report are deterministic
    assert 'retries=MAX_RUN_RETRIES if stage == "run" else 0' in SRC
    assert 'backoff=RUN_BACKOFF_S if stage == "run" else None' in SRC
    # and the run stage waits LONGER than the others, because it is the
    # expensive one to lose: nq-s06-03 died at trial 36 of 100 when a
    # substitution outlasted the 28-minute generic budget.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_rs", REPO_ROOT / "scripts" / "research_session.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert sum(m.RUN_BACKOFF_S) > sum(m.STAGE_BACKOFF_S)
    assert m.STAGE_BACKOFF_S == tuple(m.RUN_BACKOFF_S[:len(m.STAGE_BACKOFF_S)]), (
        "the run schedule extends the generic one rather than replacing it")
    # widening a retry budget is not the fix and the code must say so
    assert "never correctness" in FLAT


def test_a_verdict_about_a_different_family_is_never_this_sessions_result():
    """runs/l2-verdict.json is one fixed path that every L2 run overwrites. In
    session-01 the gate exited 1 and this driver read the file an earlier manual
    run had left, then printed `M = 6, best nq-orderflow-01` as the session's own
    L2 outcome. A different family's verdict, in a session record, labelled as
    its own."""
    assert 'got != sorted(names)' in SRC
    assert "NO L2 VERDICT for this session" in SRC
    assert "a different family" in SRC
    assert "verdict_file_family" in SRC
    assert "not about" in FLAT


# --------------------------------------------------------------------------- #
# a family: M declared, members not
# --------------------------------------------------------------------------- #
def _family_plan(tmp_path, iterations=3):
    import yaml
    p = tmp_path / "fam.yaml"
    p.write_text(yaml.safe_dump({
        "session": "s-fam",
        "family": {"seed": "config/hypotheses/intraday-continuation.md",
                   "iterations": iterations, "n": 5, "prefix": "nq-fam"}}))
    return p


def test_a_family_declares_M_but_not_its_members(tmp_path):
    """The count is what the correction needs -- multiplicity is the number of
    looks. Which question each campaign asks is decided as it goes, and saying
    that plainly beats implying the family was written in advance."""
    proc = subprocess.run(
        [sys.executable, "scripts/research_session.py", "--plan",
         str(_family_plan(tmp_path)), "--dry-run"],
        capture_output=True, text=True, cwd=str(REPO_ROOT))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "M = 5" in proc.stdout or "M = 3" in proc.stdout
    assert "decided as it goes" in proc.stdout
    assert "the L2 family would be: nq-fam-01, nq-fam-02, nq-fam-03" in proc.stdout
    assert "not written yet" in proc.stdout, (
        "a campaign with no question yet must say so, not print None")


def test_a_plan_is_a_list_or_a_family_but_not_both(tmp_path):
    import yaml
    p = tmp_path / "both.yaml"
    p.write_text(yaml.safe_dump({
        "session": "x",
        "campaigns": [{"name": "a", "hypothesis": "h.md"}],
        "family": {"seed": "s.md", "iterations": 3}}))
    proc = subprocess.run(
        [sys.executable, "scripts/research_session.py", "--plan", str(p)],
        capture_output=True, text=True, cwd=str(REPO_ROOT))
    assert proc.returncode != 0
    assert "not both" in (proc.stdout + proc.stderr)


def test_a_family_of_one_is_refused(tmp_path):
    proc = subprocess.run(
        [sys.executable, "scripts/research_session.py", "--plan",
         str(_family_plan(tmp_path, iterations=1))],
        capture_output=True, text=True, cwd=str(REPO_ROOT))
    assert proc.returncode != 0
    assert "at least 2" in (proc.stdout + proc.stderr)


def test_the_refinement_happens_inside_phase_1():
    """That is what makes it legal. refine_hypothesis.py refuses if a validation
    artefact is readable, and during phase 1 none exists because validation runs
    at the stop. The ordering is the enforcement, not a promise."""
    p1 = SRC.index("phase 1: campaigns")
    p2 = SRC.index("phase 2: validation at the stop")
    assert "refine_hypothesis.py" in SRC[p1:p2]
    assert "refine_hypothesis.py" not in SRC[p2:]
    assert "ordering enforces" in FLAT or "ordering is the enforcement" in FLAT


def test_a_failed_refinement_never_reuses_the_parent_question():
    """No question means no campaign. Carrying on with the parent would silently
    make two campaigns the same experiment and inflate M by one look that was
    really the same look."""
    assert "refinement FAILED" in SRC
    assert "question for the next campaign" in SRC
    assert "no question: the refinement" in SRC
    # and it aborts rather than quietly continuing. `_refine_after` now returns
    # that flag instead of assigning it, because the resume path calls the same
    # function -- so the assertion is on the return and on both call sites
    # binding it, which is what actually stops the loop.
    assert "return parent, True" in SRC
    assert SRC.count("aborted_by_failure = _refine_after(") == 2


def test_an_infrastructure_failure_does_not_spend_validation_looks():
    """A cue is a decision; a timeout is not, and they must not share a path.

    On 2026-09-30 a single ReadTimeout on the gateway preflight ended a
    five-campaign family after one campaign -- and because a failed refinement
    set the same flag a human cue sets, the session went to the stop and spent
    nq-s02-01's one validation look on a plan that never ran.
    """
    assert "aborted_by_failure" in SRC
    assert "STOPPING WITHOUT VALIDATING" in SRC
    assert "timeout is not, and the two must not share" in FLAT
    # the failure path must not set the cue flag
    i = SRC.index("refinement FAILED")
    j = SRC.index("print(f\"\\n  the refinement after {name} failed")
    assert "stopped_by_cue = True" not in SRC[i:j + 1200]
    # and phases 2-4 are skipped outright
    assert 'log["validation"] = "NOT RUN -- the session aborted on a failure"' in SRC
    assert "phases 2-4 were SKIPPED" in SRC


def test_the_refinement_retries_like_every_other_stage():
    """It was left out of the retry helper, and one timeout cost a session."""
    assert "_retrying(" in SRC
    # The refine call is built as `refine_argv` and then handed to `_retrying`,
    # so assert on the pairing rather than on their distance apart.
    assert "refine_argv = [PY" in SRC
    # The call itself, however many plan keys are added to the argv above it.
    call = SRC[SRC.index("_retrying(refine_argv"):]
    call = call[:call.index(")\n") + 1]
    assert "retries=MAX_STAGE_RETRIES" in call
    assert "ReadTimeout" in SRC and "ConnectionError" in SRC


# --------------------------------------------------------------------------- #
# Task 72: the control arm, and not spending looks to audit a process
# --------------------------------------------------------------------------- #
def _control_plan(tmp_path, refine=False, iterations=3):
    import yaml
    p = tmp_path / "ctl.yaml"
    p.write_text(yaml.safe_dump({
        "session": "s-ctl",
        "family": {"seed": "config/hypotheses/intraday-continuation.md",
                   "iterations": iterations, "refine": refine, "n": 5,
                   "prefix": "nq-ctl"}}))
    return p


def test_a_control_family_runs_the_same_question_every_time(tmp_path):
    """Three independent best-of-N draws from one fixed question give the spread
    that any claim about refinement has to clear. Without it, nq-s03's
    0.0786 -> 0.7530 -> 0.8924 is uninterpretable."""
    proc = subprocess.run(
        [sys.executable, "scripts/research_session.py", "--plan",
         str(_control_plan(tmp_path)), "--train-only", "--dry-run"],
        capture_output=True, text=True, cwd=str(REPO_ROOT))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "CONTROL ARM" in proc.stdout
    assert proc.stdout.count("intraday-continuation.md") >= 3, (
        "every campaign must name the SAME question")
    assert "not written yet" not in proc.stdout
    assert "run-to-run spread" in proc.stdout


def test_a_control_family_never_calls_the_refiner():
    # The guard moved into `_refine_after` and is stated as an early return, so
    # it reads inverted. Same three conditions, same meaning: a control family
    # (`refine: false`) never reaches the refiner.
    assert 'if not (fam and refines and name != entries[-1]["name"]):' in SRC
    assert "return parent, False" in SRC
    assert 'refines = fam.get("refine", True)' in SRC
    assert "`refine: false` is the control arm" in FLAT


def test_train_only_skips_every_phase_that_touches_a_closed_split(tmp_path):
    """Validation is one look per campaign. Spending six on a methodology
    experiment is the same mistake as opening the holdout to test plumbing."""
    proc = subprocess.run(
        [sys.executable, "scripts/research_session.py", "--plan",
         str(_control_plan(tmp_path)), "--train-only", "--dry-run"],
        capture_output=True, text=True, cwd=str(REPO_ROOT))
    assert "phases 2-4 would be skipped" in proc.stdout
    assert "no campaign would be validated" in proc.stdout
    assert '"--train-only"' in SRC
    assert "phases 2-4 SKIPPED" in SRC
    assert "same mistake as opening the holdout" in FLAT


def test_train_only_and_arming_the_holdout_are_refused_together(tmp_path):
    proc = subprocess.run(
        [sys.executable, "scripts/research_session.py", "--plan",
         str(_control_plan(tmp_path)), "--train-only",
         "--holdout-if-l2-passes", "nope"],
        capture_output=True, text=True, cwd=str(REPO_ROOT))
    assert proc.returncode != 0
    assert "contradict each other" in (proc.stdout + proc.stderr)


def test_the_task72_plans_are_identical_except_for_refine():
    """The arms differ in ONE field. If anything else drifts -- N, the proposer,
    the profile, the seed -- the comparison measures that instead."""
    import yaml
    a = yaml.safe_load((REPO_ROOT / "config" / "sessions"
                        / "task72-refined.yaml").read_text())["family"]
    b = yaml.safe_load((REPO_ROOT / "config" / "sessions"
                        / "task72-control.yaml").read_text())["family"]
    assert a.pop("refine") is True and b.pop("refine") is False
    assert a.pop("prefix") != b.pop("prefix")
    assert a == b, f"the arms differ in more than `refine`: {a} vs {b}"


def test_the_refiner_keeps_its_own_model_rather_than_the_proposers():
    """The driver used to pass the campaign's model to the refiner, so
    `refine_hypothesis`'s own choice could never take effect inside a session --
    the refiner silently ran on whatever wrote the strategies.

    It is the same shape as the committee defect: `nq_campaign.py` passes
    `(record.agent_config).get("model")` into `review()`, so the auditors inherit
    the proposer's model too. Judgement should not default to the model being
    judged."""
    assert 'refine_argv = [PY, "scripts/refine_hypothesis.py"' in SRC
    assert '"--model", str(c.get("model")' not in SRC, (
        "the campaign's model must not reach the refiner")
    assert 'if fam.get("refine_model"):' in SRC, "but a plan may override it"
    assert "Do NOT pass the campaign's model" in SRC


def test_a_session_that_produced_nothing_exits_non_zero():
    """**It returned 0, so a session where NOTHING ran reported success.** That
    is how the first rehearsal printed "session driver exited 0" over a session
    that died at its very first register. For anything automated -- cron, CI, a
    human reading `$?` -- a failure that reads as a pass is the same class of
    fault as the ones that cost validation looks.

    The rule is this codebase's own, from the refinement-failure path: a cue is a
    decision and a failure is not. A human who stopped the session got what they
    asked for; a session that produced nothing for any other reason did not do
    its job, whatever its log says.
    """
    assert "return 0 if stopped_by_cue else 1" in SRC
    assert "return 0 if (completed or stopped_by_cue) else 1" in SRC, (
        "--train-only asks for phases 2-4 to be skipped, not for zero campaigns")
    flat = " ".join(SRC.split())
    assert "no campaign completed, and nothing asked for that" in flat
    # and the reason is in the record, not only on stdout
    assert 'log["produced_nothing"]' in SRC


# --------------------------------------------------------------------------- #
# a statement instead of a file (Amendment 15)
# --------------------------------------------------------------------------- #
def _dry(plan, *extra):
    return subprocess.run(
        [sys.executable, "scripts/research_session.py", "--plan", str(plan),
         "--dry-run", *extra], capture_output=True, text=True, cwd=str(REPO_ROOT))


def test_a_list_campaign_may_name_a_statement(tmp_path):
    import yaml
    p = tmp_path / "q.yaml"
    p.write_text(yaml.safe_dump({"session": "s-q", "campaigns": [
        {"name": "nq-q-01", "question": "CVD divergence marks trend exhaustion"},
        {"name": "nq-q-02", "hypothesis": "config/hypotheses/intraday-continuation.md"}]}))
    proc = _dry(p)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert '(written from: "CVD divergence marks trend exhaustion")' in proc.stdout
    assert "intraday-continuation.md" in proc.stdout


def test_a_family_may_start_from_a_statement(tmp_path):
    import yaml
    p = tmp_path / "fq.yaml"
    p.write_text(yaml.safe_dump({"session": "s-fq", "family": {
        "seed_question": "CVD divergence marks trend exhaustion",
        "iterations": 3, "n": 5, "prefix": "nq-fq"}}))
    proc = _dry(p)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.count("written from:") == 1, "only campaign 1 writes it"
    assert proc.stdout.count("not written yet") == 2


def test_a_control_family_from_a_statement_reuses_one_question(tmp_path):
    import yaml
    p = tmp_path / "cq.yaml"
    p.write_text(yaml.safe_dump({"session": "s-cq", "family": {
        "seed_question": "CVD divergence marks trend exhaustion",
        "iterations": 3, "refine": False, "n": 5, "prefix": "nq-cq"}}))
    proc = _dry(p, "--train-only")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.count("written from:") == 1
    assert proc.stdout.count("the same question, written for the first") == 2
    assert "not written yet" not in proc.stdout


@pytest.mark.parametrize("fam", [
    {"seed": "config/hypotheses/intraday-continuation.md", "seed_question": "x"},
    {},
])
def test_a_family_names_exactly_one_seed(tmp_path, fam):
    import yaml
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump({"session": "s", "family": {
        **fam, "iterations": 3, "prefix": "nq-b"}}))
    proc = _dry(p)
    assert proc.returncode != 0
    assert "exactly one of seed" in (proc.stdout + proc.stderr)


def test_a_written_question_is_read_back_from_the_record_not_stdout():
    """The record is what the campaign ran on; the writer's stdout is a log."""
    body = SRC.split("def _adopt_written_question", 1)[1].split("\n    def ", 1)[0]
    assert "hypothesis_path" in body and "campaign.json" in body
    assert '"--question"' in SRC
