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
def test_it_never_takes_the_holdout_look():
    """§8 gives 73 sessions one look ever. A driver that could spend it as the
    last line of an overnight script will spend it by accident."""
    phase4 = SRC[SRC.index("phase 4: the holdout"):]
    assert '"--check"' in phase4
    assert "--reason" not in phase4.split("a human runs")[0], (
        "the driver must not be able to pass --reason to holdout.py")
    assert "never taken by a script" in FLAT
    assert "spend it by accident" in FLAT


def test_the_family_is_the_plan_and_the_plan_is_written_first():
    """Dropping the campaigns that did badly shrinks M and inflates the maximum's
    significance. The family is every campaign named in the plan, and the plan is
    copied into the session record."""
    assert '"declared_family": names' in SRC
    assert '"--campaigns", ",".join(names)' in SRC
    assert "written before the session" in FLAT.lower()


def test_it_does_not_invent_hypotheses():
    """Amendment 8 makes the commit the human act, so a session cannot distil a
    question and register it in one breath."""
    assert "will not invent hypotheses" in FLAT
    assert "Amendment 8" in FLAT
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
