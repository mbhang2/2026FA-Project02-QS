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
