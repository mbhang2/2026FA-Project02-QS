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
                  free_disk=500 * GIB, per_trial=GIB, nofile=1048576,
                  history=(1, 0), concurrency=None):
        # History and measured concurrency are part of the machine as far as the
        # planner is concerned, so the fixture owns them. Without this the real
        # ledger's 1,404 completed trials at W=4 leak into every unit test and
        # the fixture stops describing the machine it claims to.
        monkeypatch.setattr(capacity, "proven_by_history", lambda root=None: history)
        monkeypatch.setattr(capacity, "concurrency_costs",
                            lambda profile: concurrency or {})
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


def test_the_memory_model_warns_but_cannot_refuse(machine):
    """**The correction this module exists to pin.** The first planner made
    `W * peak_rss` a refusing constraint and it refused W=4 on a machine whose
    ledger holds 1,404 completed trials at W=4. Measuring showed the model is
    not conservative but wrong in kind: RSS is per-process and does not add,
    because the interpreter, the linked libraries and the memory-mapped tick
    cache are resident in every trial and exist once in RAM. One trial dropped
    system available memory by 1.12 GiB; four concurrent dropped it by 0.77 GiB,
    against a prediction of 3.44 GiB.

    So it reports, and it never decides. The alternative was raising `headroom`
    until W=4 passed, which is fitting the threshold to the wanted answer."""
    machine(cores_fast=8, available=4 * GIB, per_trial=GIB)
    p = capacity.plan(8, "nq-train")
    model = next(c for c in p["constraints"] if c["name"] == "memory (model)")
    assert model["advisory"] is True
    assert model["fits"] == 3, "the model should still be computed and shown"
    assert p["binding"] != "memory (model)", (
        "the advisory memory model is deciding the verdict again")
    assert p["max_width"] == 8 and p["verdict"] == "OK"


def test_a_measured_concurrency_cost_is_reported(machine):
    machine(cores_fast=8, available=32 * GIB, per_trial=GIB,
            concurrency={4: {"drop": 0.77 * GIB, "wall_s": 358.0}})
    p = capacity.plan(4, "nq-train")
    measured = [c for c in p["constraints"] if c["name"].startswith("memory (measured")]
    assert len(measured) == 1
    assert "observed system drop" in measured[0]["detail"]


def test_cores_bind_when_they_are_the_scarce_thing(machine):
    machine(cores_fast=4, available=256 * GIB, per_trial=GIB)
    p = capacity.plan(8, "nq-train")
    assert p["binding"] == "cores"
    assert p["max_width"] == 4
    assert p["verdict"] == "REFUSE"


def test_a_width_the_ledger_has_carried_is_never_refused(machine):
    """Inference loses to a record. This is the specific failure that happened:
    the planner said 3, the machine had completed 1,404 trials at 4."""
    machine(cores_fast=2, available=4 * GIB, per_trial=GIB, history=(4, 1404))
    p = capacity.plan(4, "nq-train")
    assert p["verdict"] == "OK"
    assert p["max_width"] == 4
    assert p["model_max_width"] == 2
    assert "1404 trials completed" in p["max_width_basis"]


def test_one_lucky_row_is_not_evidence(machine):
    """A single completed row could be a trial that landed while the machine
    was quiet. A campaign's worth of them is the machine doing it repeatedly."""
    machine(cores_fast=2, available=4 * GIB, history=(8, 3))
    p = capacity.plan(8, "nq-train")
    assert p["verdict"] == "REFUSE"
    assert p["max_width"] == 2


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
    p = capacity.plan(4, "nq-train")
    src = _no_comments((REPO_ROOT / "scripts" / "capacity.py").read_text())
    budget = src.split("budget =", 1)[1].split("\n", 1)[0]
    assert "swap" not in budget, f"swap entered the memory budget: {budget!r}"
    model = next(c for c in p["constraints"] if c["name"] == "memory (model)")
    assert model["fits"] == 3, "the advisory figure is still computed from RAM only"


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
    # Refused on CORES, which is a sound constraint. Memory is advisory now and
    # cannot refuse, and the real ledger's W=4 history would lift the refusal
    # anyway -- so a test that fakes memory small proves nothing.
    monkeypatch.setattr(capacity, "proven_by_history", lambda root=None: (1, 0))
    monkeypatch.setattr(capacity, "cores", lambda: {
        "total": 2, "usable": 2, "usable_basis": "test", "performance": 2,
        "efficiency": 0, "cgroup_quota": None, "brand": "test",
        "schedulable": 2})
    monkeypatch.setattr(capacity, "footprint", lambda profile: {
        "profile": profile, "peak_rss_bytes": GIB, "wall_s": 300.0,
        "measured_at": "2026-10-11T00:00:00+00:00"})

    with pytest.raises(SystemExit) as e:
        nq_campaign.stage_run("nq-cap-test")
    msg = str(e.value)
    assert "refusing to start" in msg and "W=4" in msg
    assert "NOT being lowered" in msg

    written = json.loads((dst / "capacity.json").read_text())
    assert written["verdict"] == "REFUSE"
    assert written["ignored"] is None
    assert written["binding"] == "cores"


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
    monkeypatch.setattr(capacity, "proven_by_history", lambda root=None: (1, 0))
    monkeypatch.setattr(capacity, "cores", lambda: {
        "total": 2, "usable": 2, "usable_basis": "test", "performance": 2,
        "efficiency": 0, "cgroup_quota": None, "brand": "test",
        "schedulable": 2})
    monkeypatch.setattr(capacity, "footprint", lambda profile: {
        "profile": profile, "peak_rss_bytes": GIB, "wall_s": 300.0,
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
def test_decide_stops_at_the_widest_width_actually_known_good(machine):
    """Without this the planner would answer 8 here -- cores allow it, and
    memory can no longer refuse -- recommending a width whose isolation was
    never proven and which has never completed a trial, on the strength of a
    model just caught being wrong in the other direction."""
    machine(cores_fast=16, cores_total=16, available=4 * GIB, per_trial=GIB,
            history=(4, 1404))
    d = capacity.decide("nq-train", measure_if_missing=False)
    assert d["width"] == capacity.PROVEN_WIDTH == 8
    assert "known-good ceiling" in d["binding"]
    assert d["plan"]["max_width"] == 16
    assert "--prove" in d["headroom_note"]
    assert "--ceiling 16" in d["headroom_note"]


def test_decide_never_falls_below_the_proven_width(machine):
    """With no history at all the floor is the one width whose isolation was
    proven, so a fresh machine is not pushed down to the chain."""
    machine(cores_fast=8, available=4 * GIB, per_trial=GIB, history=(1, 0))
    d = capacity.decide("nq-train", measure_if_missing=False)
    assert d["width"] == capacity.PROVEN_WIDTH


def test_an_explicit_ceiling_widens_as_well_as_narrows(machine):
    machine(cores_fast=8, available=256 * GIB, per_trial=GIB, history=(4, 1404))
    assert capacity.decide("nq-train", ceiling=2,
                           measure_if_missing=False)["width"] == 2
    wide = capacity.decide("nq-train", ceiling=8, measure_if_missing=False)
    assert wide["width"] == 8, "an explicit ceiling cannot widen past known-good"
    # With the ceiling at the sound cap, the sound constraint is what limits --
    # the ceiling stopped being the thing in the way.
    assert wide["binding"] == "cores"


def test_decide_cannot_exceed_a_sound_constraint_even_with_a_ceiling(machine):
    machine(cores_fast=2, available=256 * GIB, per_trial=GIB, history=(1, 0))
    d = capacity.decide("nq-train", ceiling=16, measure_if_missing=False)
    assert d["width"] == 2, (
        "a caller's ceiling overrode the core count; a ceiling raises the cap, "
        "it does not invent capacity")


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


def test_decide_says_so_when_a_ceiling_pushes_past_the_proven_width(machine):
    """Isolation has only ever been proven at 4. Going wider is allowed --
    `stage_run` verifies isolation every time -- but the record must not imply
    the equivalence proof covered 8 when it covered 4."""
    machine(cores_fast=16, cores_total=16, available=256 * GIB, per_trial=GIB)
    d = capacity.decide("nq-train", ceiling=12, measure_if_missing=False)
    assert d["width"] == 12 > capacity.PROVEN_WIDTH
    assert "unproven_width" in d and "--prove" in d["unproven_width"]
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


def test_a_measurement_that_fit_is_evidence_and_never_a_cap(machine):
    """Treating a fitting measurement as a cap made a k=1 measurement pin W to
    1: the planner would have recommended the sequential chain to anyone who
    measured a single trial. A measurement that fit says k works, not that k is
    the ceiling."""
    machine(cores_fast=8, available=256 * GIB, per_trial=GIB, history=(1, 0),
            concurrency={1: {"drop": 0.48 * GIB, "wall_s": 296.0},
                         4: {"drop": 1.28 * GIB, "wall_s": 346.0}})
    p = capacity.plan(8, "nq-train")
    assert p["max_width"] == 8, (
        f"a fitting measurement capped the plan at {p['max_width']}")
    for c in p["constraints"]:
        if c["name"].startswith("memory (measured"):
            assert c["advisory"] is True


def test_a_measurement_that_did_not_fit_does_cap(machine):
    """The other direction has to work, or the measurement is decoration."""
    machine(cores_fast=8, available=2 * GIB, per_trial=GIB, history=(1, 0),
            concurrency={4: {"drop": 8 * GIB, "wall_s": 346.0}})
    p = capacity.plan(4, "nq-train")
    assert p["binding"] == "memory (measured W=4)"
    assert p["max_width"] == 3
    assert p["verdict"] == "REFUSE"


# --------------------------------------------------------------------------- #
# fitting is not the same as being worth using
# --------------------------------------------------------------------------- #
def test_throughput_is_trials_per_second_not_width(machine):
    machine(concurrency={1: {"drop": GIB, "wall_s": 300.0},
                         4: {"drop": GIB, "wall_s": 330.0},
                         8: {"drop": GIB, "wall_s": 700.0}})
    t = capacity.throughput("nq-train")
    assert t[1] == pytest.approx(1 / 300)
    assert t[4] == pytest.approx(4 / 330)
    assert t[8] == pytest.approx(8 / 700)
    assert t[4] > t[8], "the measured case: four is faster per trial than eight"


def test_a_tie_resolves_to_the_narrower_width(machine):
    """**The measured case on this machine.** W=8 reproduces the sequential
    score exactly and completes eight trials in 653-778 s against four in 330 s,
    so throughput is flat. A tie goes to the narrower width: it pages less, has
    the longer track record, and loses less when a trial dies."""
    machine(cores_fast=8, available=256 * GIB, per_trial=GIB,
            concurrency={4: {"drop": GIB, "wall_s": 330.0},
                         8: {"drop": GIB, "wall_s": 653.0}})
    fast, table = capacity.fastest_width("nq-train")
    assert table[8] >= table[4] * (1 - capacity.THROUGHPUT_MARGIN), (
        "these two should be within the margin; otherwise this tests nothing")
    assert fast == 4
    d = capacity.decide("nq-train", measure_if_missing=False)
    assert d["width"] == 4
    assert "measured throughput" in d["binding"]


def test_a_clearly_faster_wider_width_does_win(machine):
    """The margin must not become a reason to never parallelise."""
    machine(cores_fast=8, available=256 * GIB, per_trial=GIB,
            concurrency={2: {"drop": GIB, "wall_s": 300.0},
                         8: {"drop": GIB, "wall_s": 330.0}})
    assert capacity.fastest_width("nq-train")[0] == 8
    assert capacity.decide("nq-train", measure_if_missing=False)["width"] == 8


def test_one_measurement_is_not_a_comparison(machine):
    """With a single width timed there is nothing to compare, so the ceiling
    fallback has to carry it rather than a throughput claim from one point."""
    machine(cores_fast=16, cores_total=16, available=256 * GIB, per_trial=GIB,
            history=(4, 1404), concurrency={4: {"drop": GIB, "wall_s": 330.0}})
    assert capacity.fastest_width("nq-train")[0] is None
    d = capacity.decide("nq-train", measure_if_missing=False)
    assert "known-good ceiling" in d["binding"]
    assert "--measure-concurrency" in d["binding"]


def test_throughput_cannot_exceed_a_sound_constraint(machine):
    machine(cores_fast=2, available=256 * GIB, per_trial=GIB,
            concurrency={2: {"drop": GIB, "wall_s": 300.0},
                         8: {"drop": GIB, "wall_s": 300.0}})
    d = capacity.decide("nq-train", measure_if_missing=False)
    assert d["width"] == 2, "throughput invented capacity the machine lacks"
    assert d["binding"] == "cores"


def test_an_explicit_ceiling_overrides_the_throughput_judgement(machine):
    """A caller naming a width has a reason the measurements do not know."""
    machine(cores_fast=8, available=256 * GIB, per_trial=GIB,
            concurrency={4: {"drop": GIB, "wall_s": 330.0},
                         8: {"drop": GIB, "wall_s": 653.0}})
    d = capacity.decide("nq-train", ceiling=8, measure_if_missing=False)
    assert d["width"] == 8, "the ceiling did not override the throughput advice"
    # Throughput alone would have cut this to 4; the ceiling is why it did not.
    assert "throughput" not in d["binding"]
    assert capacity.decide("nq-train", measure_if_missing=False)["width"] == 4


def test_the_decision_records_the_throughput_it_used(machine):
    machine(concurrency={4: {"drop": GIB, "wall_s": 330.0},
                         8: {"drop": GIB, "wall_s": 653.0}})
    d = capacity.decide("nq-train", measure_if_missing=False)
    assert set(d["throughput_trials_per_s"]) == {"4", "8"}


def test_proven_width_is_about_correctness_and_not_speed():
    """`PROVEN_WIDTH` moved to 8 because `--equivalence` showed 8/8 runs
    bit-identical to sequential in two reps. It did NOT move because 8 is fast;
    the same run showed it is not. Keeping the two separate is the point: safety
    is proven by equivalence, and the width to use is chosen by throughput."""
    assert capacity.PROVEN_WIDTH == 8
    src = (REPO_ROOT / "scripts" / "capacity.py").read_text()
    note = src.split("PROVEN_WIDTH = 8", 1)[0][-1400:]
    assert "equivalence" in note and "bit-identical" in note
    assert "no throughput gained" in note or "3.73x" in note, (
        "the constant no longer records that the wider width bought nothing")
