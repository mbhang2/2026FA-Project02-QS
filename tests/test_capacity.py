"""The conditioning stage: what the machine can carry, decided before a run.

Brian asked for a pre-run script that finds how many cores and how much memory
the device can allocate and uses what it has. The part that needs testing is not
the arithmetic -- it is the two places where doing the obvious thing would be
wrong:

  * `generation_width` is pre-registered and decides what each agent SEES, so
    the planner must REFUSE a W the machine cannot carry rather than lowering
    it. A quietly narrowed run is a different experiment with the same id.
  * a per-trial footprint must be MEASURED. Guessing one would turn a safety
    check into a second way to lose a three-day run.
"""

from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import capacity  # noqa: E402

GIB = 1073741824


def _no_comments(src: str) -> str:
    """Assertions below are about code, and this repository's comments quote the
    code they explain. Stripping them first is why these tests do not pass on
    an explanation of the behaviour they are checking."""
    return re.sub(r"^\s*#.*$", "", src, flags=re.M)


# --------------------------------------------------------------------------- #
# it refuses to guess
# --------------------------------------------------------------------------- #
def test_a_missing_measurement_is_unknown_and_not_a_number(monkeypatch):
    monkeypatch.setattr(capacity, "footprint", lambda profile: None)
    p = capacity.plan(8, "nq-train")
    assert p["verdict"] == "UNKNOWN"
    assert "max_width" not in p, (
        "a plan with no measured footprint produced a width anyway; that number "
        "would be invented")
    assert "--measure" in p["reason"]


def test_the_measurement_spends_a_check_trial_so_n_is_untouched():
    """`runlog.trial_rows` filters `purpose == "trial"`, so a check row is
    outside N. If the measuring run ever became a trial it would consume budget
    from the campaign it is supposed to protect."""
    src = _no_comments((REPO_ROOT / "scripts" / "capacity.py").read_text())
    body = src.split("def measure_trial", 1)[1].split("\ndef ", 1)[0]
    assert '"--purpose", "check"' in body, (
        "the measuring trial no longer declares purpose=check and would count "
        "toward N")
    assert "--unlock-holdout" not in body


def test_the_footprint_is_keyed_to_the_machine_that_measured_it():
    """A number measured on a 10-core laptop is not a fact about a 64-core box."""
    assert len(capacity.machine_id()) == 16
    calls = []
    real = capacity.cores

    def other_machine():
        c = dict(real())
        c["brand"] = "Some Other CPU"
        return c

    first = capacity.machine_id()
    capacity.cores = other_machine
    try:
        assert capacity.machine_id() != first
    finally:
        capacity.cores = real


# --------------------------------------------------------------------------- #
# the binding constraint
# --------------------------------------------------------------------------- #
@pytest.fixture
def machine(monkeypatch):
    """A machine whose every dimension can be set independently."""
    def configure(*, cores_fast=8, cores_total=10, available=32 * GIB,
                  free_disk=500 * GIB, per_trial=GIB, nofile=1048576):
        monkeypatch.setattr(capacity, "cores", lambda: {
            "total": cores_total, "usable": cores_total,
            "usable_basis": "test", "performance": cores_fast,
            "efficiency": cores_total - cores_fast, "cgroup_quota": None,
            "brand": "test", "schedulable": cores_fast})
        monkeypatch.setattr(capacity, "memory", lambda: {
            "total": 64 * GIB, "available": available,
            "available_basis": "test", "cgroup_limit": None,
            "swap_total": 8 * GIB, "swap_used": 0})
        monkeypatch.setattr(capacity, "disk", lambda path=None: {
            "total": 1000 * GIB, "free": free_disk, "used_pct": 50.0})
        monkeypatch.setattr(capacity, "limits", lambda: {
            "RLIMIT_NOFILE": {"soft": nofile, "hard": nofile, "unlimited": False}})
        monkeypatch.setattr(capacity, "footprint", lambda profile: {
            "profile": profile, "peak_rss_bytes": per_trial, "wall_s": 300.0,
            "measured_at": "2026-10-11T00:00:00+00:00"})
    return configure


def test_memory_binds_when_it_is_the_scarce_thing(machine):
    machine(cores_fast=8, available=4 * GIB, per_trial=GIB)
    p = capacity.plan(8, "nq-train")
    assert p["binding"] == "memory"
    assert p["max_width"] == 3            # 4 GiB * 0.85 headroom // 1 GiB
    assert p["verdict"] == "REFUSE"


def test_cores_bind_when_memory_is_plentiful(machine):
    machine(cores_fast=4, available=256 * GIB, per_trial=GIB)
    p = capacity.plan(8, "nq-train")
    assert p["binding"] == "cores"
    assert p["max_width"] == 4


def test_disk_can_bind_even_though_it_never_has_here(machine):
    machine(cores_fast=8, available=256 * GIB, free_disk=16 * 1048576)
    p = capacity.plan(4, "nq-train")
    assert p["binding"] == "disk"
    assert p["max_width"] == 2


def test_the_descriptor_limit_is_a_constraint_too(machine):
    machine(cores_fast=8, available=256 * GIB, nofile=128)
    p = capacity.plan(4, "nq-train")
    assert p["binding"] == "open files"
    assert p["max_width"] == 2


def test_width_one_is_never_refused(machine):
    """The sequential path is the one eighteen campaigns ran. A capacity check
    that can refuse W=1 would be a check that can stop all work on a loaded
    laptop, and it would be refusing the configuration that uses least."""
    machine(cores_fast=1, available=1024, free_disk=1024, per_trial=64 * GIB)
    p = capacity.plan(1, "nq-train")
    assert p["max_width"] == 1
    assert p["verdict"] == "OK"


def test_a_carryable_width_passes(machine):
    machine(cores_fast=8, available=32 * GIB, per_trial=GIB)
    p = capacity.plan(4, "nq-train")
    assert p["verdict"] == "OK"
    assert p["max_width"] == 8


# --------------------------------------------------------------------------- #
# reading the machine honestly
# --------------------------------------------------------------------------- #
def test_efficiency_cores_are_not_counted_as_parallel_lanes():
    """A generation finishes when its slowest worker finishes, so a trial parked
    on an efficiency core sets the wall clock for the whole generation. On this
    M1 Pro that is 8 schedulable, not the 10 `os.cpu_count()` reports."""
    c = capacity.cores()
    assert c["schedulable"] <= c["total"]
    if c["performance"]:
        assert c["schedulable"] == c["performance"]
        assert c["efficiency"] is not None and c["efficiency"] >= 0


def test_swap_is_reported_but_never_counted_as_capacity(machine):
    """A trial that fits only by paging still trips the engine's wall-clock
    budget. Swap belongs in the warning, not the budget."""
    machine(available=4 * GIB, per_trial=GIB)
    narrow = capacity.plan(4, "nq-train")["max_width"]
    src = _no_comments((REPO_ROOT / "scripts" / "capacity.py").read_text())
    budget = src.split("budget =", 1)[1].split("\n", 1)[0]
    assert "swap" not in budget, f"swap entered the memory budget: {budget!r}"
    assert narrow == 3


def test_ru_maxrss_units_follow_the_platform():
    """`ru_maxrss` is bytes on Darwin and kilobytes on Linux. Getting it
    backwards is a factor of 1024 -- in the direction that reports a trial
    needing 880 GiB, or one needing 840 KiB."""
    expected = 1 if platform.system() == "Darwin" else 1024
    assert capacity._RU_MAXRSS_TO_BYTES == expected


def test_a_cgroup_memory_limit_beats_the_hosts_own_number(tmp_path, monkeypatch):
    """Scored campaigns are meant to run on Linux, where the number that kills
    the process is the cgroup's, not the host's."""
    src = _no_comments((REPO_ROOT / "scripts" / "capacity.py").read_text())
    body = src.split("def memory", 1)[1].split("\ndef ", 1)[0]
    assert "/sys/fs/cgroup/memory.max" in body
    assert "memory.limit_in_bytes" in body, "cgroup v1 is not consulted"
    assert "MemAvailable" in body, (
        "MemTotal would be read instead of MemAvailable, which counts memory "
        "the kernel cannot actually hand over")
    cores_body = src.split("def cores", 1)[1].split("\ndef ", 1)[0]
    assert "sched_getaffinity" in cores_body
    assert "cpu.max" in cores_body, "a container's CPU quota is ignored"


def test_the_probe_reports_without_needing_a_measurement():
    out = subprocess.run([sys.executable, "scripts/capacity.py", "--probe"],
                         cwd=REPO_ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    for field in ("cores", "memory", "disk", "machine"):
        assert field in out.stdout


# --------------------------------------------------------------------------- #
# the gate in the campaign driver
# --------------------------------------------------------------------------- #
GATE = (REPO_ROOT / "scripts" / "nq_campaign.py").read_text()


def test_the_gate_refuses_and_does_not_silently_lower_w():
    """The one thing this must never do. `shown` is snapshotted once per
    generation, so W decides what each agent sees: at W=4 trials 0-3 propose
    blind and 4-7 see 0-3, while at W=8 all eight propose blind. Lowering W to
    fit the hardware would make the search path a property of the laptop."""
    body = _no_comments(GATE).split("def stage_run", 1)[1].split("\ndef ", 1)[0]
    gate = body.split("capacity_mod.plan", 1)[1][:1200]
    assert "sys.exit(" in gate, "the gate no longer stops the run"
    assert not re.search(r"width\s*=\s*(?!max\(1, int\(record)", gate), (
        "something in the gate reassigns width; it must refuse, not resize")


def test_the_gate_runs_before_any_agent_is_built():
    """It is a *pre-run* stage. Failing after the agents exist wastes the model
    objects and, once `propose` is reached, real calls."""
    body = _no_comments(GATE).split("def stage_run", 1)[1].split("\ndef ", 1)[0]
    assert body.index("capacity_mod.plan") < body.index("agent_for("), (
        "the capacity check moved after agent construction")
    assert body.index("capacity_mod.plan") < body.index("worker_tree.create"), (
        "workers are built before capacity is checked")


def test_the_override_is_recorded_rather_than_a_bare_flag():
    """`--ignore-capacity` takes a reason for the same purpose the holdout
    unlock does: the record should say why someone overrode the machine."""
    assert '"--ignore-capacity", default=None, metavar="REASON"' in GATE
    body = _no_comments(GATE).split("def stage_run", 1)[1].split("\ndef ", 1)[0]
    assert '"ignored": ignore_capacity' in body, (
        "the override reason is not written into capacity.json")


def test_ignore_capacity_is_refused_on_stages_that_build_nothing():
    out = subprocess.run(
        [sys.executable, "scripts/nq_campaign.py", "--stage", "report",
         "--name", "nq-s07-02", "--ignore-capacity", "testing"],
        cwd=REPO_ROOT, capture_output=True, text=True)
    assert out.returncode != 0
    assert "--stage run only" in (out.stdout + out.stderr)


def test_the_gate_stops_a_run_it_cannot_carry(tmp_path, monkeypatch):
    """End to end on a real pre-registration, with the machine faked small."""
    import nq_campaign

    src = REPO_ROOT / "runs" / "campaigns" / "nq-s07-02"
    dst = tmp_path / "nq-cap-test"
    dst.mkdir()
    rec = json.loads((src / "campaign.json").read_text())
    rec["generation_width"] = 4
    (dst / "campaign.json").write_text(json.dumps(rec))
    shutil.copy(src / "sources" / next(iter(
        p.name for p in (src / "sources").iterdir())), dst / "champion.py")

    monkeypatch.setattr(nq_campaign, "OUT", tmp_path)
    monkeypatch.setattr(capacity, "footprint", lambda profile: {
        "profile": profile, "peak_rss_bytes": 64 * GIB, "wall_s": 300.0,
        "measured_at": "2026-10-11T00:00:00+00:00"})

    with pytest.raises(SystemExit) as e:
        nq_campaign.stage_run("nq-cap-test")
    msg = str(e.value)
    assert "refusing to start" in msg and "W=4" in msg
    assert "NOT being lowered" in msg

    written = json.loads((dst / "capacity.json").read_text())
    assert written["verdict"] == "REFUSE"
    assert written["ignored"] is None
    assert written["binding"] == "memory"


def test_the_override_lets_the_same_run_proceed(tmp_path, monkeypatch):
    """Same machine, same refusal, with a reason given: it must get past the
    gate, and the reason must land in the record.

    A sentinel is planted on the first thing `stage_run` does after the gate.
    Without it this test proceeds into agent construction and a real 300-second
    trial on real data -- which is how the first version of it took eight
    minutes and would have billed the gateway for proposals nobody reads.
    """
    import nq_campaign

    src = REPO_ROOT / "runs" / "campaigns" / "nq-s07-02"
    dst = tmp_path / "nq-cap-test2"
    dst.mkdir()
    rec = json.loads((src / "campaign.json").read_text())
    rec["generation_width"] = 4
    (dst / "campaign.json").write_text(json.dumps(rec))

    monkeypatch.setattr(nq_campaign, "OUT", tmp_path)
    monkeypatch.setattr(capacity, "footprint", lambda profile: {
        "profile": profile, "peak_rss_bytes": 64 * GIB, "wall_s": 300.0,
        "measured_at": "2026-10-11T00:00:00+00:00"})

    class PastTheGate(Exception):
        pass

    def sentinel(*a, **kw):
        raise PastTheGate

    monkeypatch.setattr(nq_campaign, "agent_for", sentinel)

    with pytest.raises(PastTheGate):
        nq_campaign.stage_run("nq-cap-test2", ignore_capacity="measuring the gate")

    written = json.loads((dst / "capacity.json").read_text())
    assert written["ignored"] == "measuring the gate"
    assert written["verdict"] == "REFUSE", (
        "the machine was faked too small to carry W=4; if this is OK the "
        "override is not being exercised at all")


# --------------------------------------------------------------------------- #
# deciding W, and the one place it is allowed to be decided
# --------------------------------------------------------------------------- #
def test_decide_picks_the_width_the_binding_constraint_allows(machine):
    machine(cores_fast=8, available=4 * GIB, per_trial=GIB)
    d = capacity.decide("nq-train", measure_if_missing=False)
    assert d["verdict"] == "OK"
    assert d["width"] == 3
    assert d["binding"] == "memory"
    assert d["plan"]["width"] == 3, (
        "the recorded plan describes a different width from the decision")


def test_decide_respects_a_ceiling(machine):
    machine(cores_fast=8, available=256 * GIB, per_trial=GIB)
    assert capacity.decide("nq-train", measure_if_missing=False)["width"] == 8
    d = capacity.decide("nq-train", ceiling=2, measure_if_missing=False)
    assert d["width"] == 2
    assert "ceiling" in d["binding"]


def test_decide_never_returns_zero(machine):
    """A machine too loaded to carry anything still has to produce a runnable
    declaration, and W=1 is the path eighteen campaigns took."""
    machine(cores_fast=1, available=1024, per_trial=64 * GIB)
    assert capacity.decide("nq-train", measure_if_missing=False)["width"] == 1


def test_decide_measures_rather_than_guessing_when_unmeasured(monkeypatch):
    monkeypatch.setattr(capacity, "footprint", lambda profile: None)
    called = []
    monkeypatch.setattr(capacity, "measure_trial",
                        lambda profile, quiet=False: called.append(profile))
    d = capacity.decide("nq-train", quiet=True)
    assert called == ["nq-train"], "decide did not measure an unmeasured machine"
    # footprint is still patched to None, so it must report UNKNOWN rather than
    # inventing a width from a measurement that did not land.
    assert d["verdict"] == "UNKNOWN" and d["width"] is None


def test_decide_says_so_when_it_picks_a_width_never_proven(machine):
    """Isolation has only ever been proven at 4 workers. Choosing 8 is allowed --
    `stage_run` verifies isolation every time -- but the record should not imply
    the equivalence proof covered it."""
    machine(cores_fast=16, cores_total=16, available=256 * GIB, per_trial=GIB)
    d = capacity.decide("nq-train", measure_if_missing=False)
    assert d["width"] > capacity.PROVEN_WIDTH
    assert "unproven_width" in d
    assert "--prove" in d["unproven_width"]
    narrow = capacity.decide("nq-train", ceiling=capacity.PROVEN_WIDTH,
                             measure_if_missing=False)
    assert "unproven_width" not in narrow


def test_the_width_flag_takes_an_integer_or_auto():
    import nq_campaign
    assert nq_campaign._width_arg("auto") == "auto"
    assert nq_campaign._width_arg("4") == 4
    for bad in ("banana", "0", "-2", "4.5"):
        with pytest.raises(BaseException):
            nq_campaign._width_arg(bad)


def test_registration_is_where_auto_is_resolved():
    """The asymmetry is the protocol. Registration may COMPUTE W because nothing
    has been spent and the number is frozen with the rest of the record; the run
    stage may only REFUSE, because by then re-deriving W would make the search
    path depend on how loaded the machine happened to be when the run started."""
    src = _no_comments(GATE)
    reg = src.split("def stage_register", 1)[1].split("\ndef ", 1)[0]
    assert "capacity_mod.decide" in reg
    assert reg.index("capacity_mod.decide") < reg.index("CampaignRecord("), (
        "W is decided after the record is built, so the record would not hold it")

    run = src.split("def stage_run", 1)[1].split("\ndef ", 1)[0]
    assert "capacity_mod.decide" not in run, (
        "the run stage decides a width; it must only plan and refuse")
    assert "capacity_mod.plan" in run


def test_the_decision_is_recorded_beside_the_record_not_inside_it():
    """`campaign.json`'s digest is re-verified by every later stage. A machine
    measurement inside it would make the pre-registration's fingerprint depend
    on how much memory was free."""
    reg = _no_comments(GATE).split("def stage_register", 1)[1].split("\ndef ", 1)[0]
    assert "capacity-at-registration.json" in reg
    assert reg.index('record.write(d / "campaign.json")') < \
        reg.index("capacity-at-registration.json")


def test_auto_is_refused_on_every_stage_but_register():
    for stage in ("run", "nulls", "validate", "report"):
        out = subprocess.run(
            [sys.executable, "scripts/nq_campaign.py", "--stage", stage,
             "--name", "nq-s07-02", "--generation-width", "auto"],
            cwd=REPO_ROOT, capture_output=True, text=True)
        assert out.returncode != 0, stage
        assert "register only" in (out.stdout + out.stderr), stage


# --------------------------------------------------------------------------- #
# one width per family
# --------------------------------------------------------------------------- #
SESSION = (REPO_ROOT / "scripts" / "research_session.py").read_text()


def test_a_session_decides_one_width_for_every_campaign_in_the_plan():
    """The L2 gate corrects over a declared family. Two members at different
    widths would be two search procedures inside one multiplicity correction,
    and per-campaign deciding would also drift downward as a laptop loads up
    over a night."""
    body = _no_comments(SESSION)
    block = body.split('if "auto" in widths', 1)[1][:1200]
    assert "capacity_mod.decide" in block
    assert block.count("capacity_mod.decide") == 1, (
        "more than one decision per plan")
    assert 'for c in entries:' in block and 'c["generation_width"]' in block, (
        "the single decision is not applied to every campaign")


def test_a_plan_that_mixes_auto_and_numbers_is_refused(tmp_path):
    plan = tmp_path / "mixed.yaml"
    plan.write_text(
        "session: cap-test\n"
        "campaigns:\n"
        "  - name: a\n    hypothesis: config/hypotheses/x.md\n"
        "    generation_width: auto\n"
        "  - name: b\n    hypothesis: config/hypotheses/x.md\n"
        "    generation_width: 4\n")
    out = subprocess.run(
        [sys.executable, "scripts/research_session.py", "--plan", str(plan)],
        cwd=REPO_ROOT, capture_output=True, text=True)
    assert out.returncode != 0
    assert "auto for all or none" in (out.stdout + out.stderr), out.stdout[-800:]


def test_the_plan_documents_auto():
    assert "generation_width: auto" in SESSION or "`auto` lets" in SESSION
