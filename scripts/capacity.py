#!/usr/bin/env python
"""What this machine can carry, decided before a run instead of during one.

Brian's point, and it has already cost this project a run: nothing checks
capacity before a campaign starts, so the machine discovers its limit by
failing. The full test suite was killed by memory pressure mid-verification on
2026-10-11, which is the cheap version of the same accident -- the expensive
version is a campaign dying on trial 300 of 400.

**What this does NOT do, deliberately.** It does not pick W for a scored
campaign. `generation_width` is pre-registered and stamped on every ledger row
because it changes the experiment, not just its wall clock:
`nq_campaign.py` snapshots `shown = list(rows)` and the champion once per
generation, so at W=4 trials 0-3 propose blind and 4-7 see 0-3, while at W=8 all
eight propose blind. Sizing W from whatever hardware is to hand would make the
search path a property of the laptop, and two runs of one pre-registration would
explore differently. So:

  * a declared W that the machine cannot carry is REFUSED, not quietly lowered
    -- running narrower is a different experiment, not a slower one;
  * W is recommended for a human to DECLARE in the next pre-registration;
  * W is auto-sized only for work that is not scored -- probes, `purpose=check`
    runs, rehearsals, worker-equivalence proofs.

**No constant here is invented.** Per-trial memory is measured by running one
real `purpose=check` trial under `getrusage` and stored per machine and per
profile. Absent a measurement for this machine, the planner refuses to answer
rather than guessing: a fabricated footprint would turn this from a safety check
into a second way to lose a run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import resource
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STORE = REPO_ROOT / "runs" / "capacity.json"
GIB = 1073741824

# `ru_maxrss` is bytes on Darwin/BSD and kilobytes on Linux. Getting this wrong
# is a factor of 1024 in the direction that says a trial needs 780 GiB.
_RU_MAXRSS_TO_BYTES = 1 if platform.system() == "Darwin" else 1024

# The widest W whose isolation has actually been proven: `worker_tree.py --prove`
# on 2026-10-04 ran 4 workers and every one reproduced the sequential score
# bit-identically. Not a cap -- a fact `decide` reports when it chooses wider.
PROVEN_WIDTH = 4


# --------------------------------------------------------------------------- #
# what the machine is
# --------------------------------------------------------------------------- #
def _sysctl(name: str) -> str | None:
    try:
        out = subprocess.run(["sysctl", "-n", name], capture_output=True,
                             text=True, timeout=5)
        return out.stdout.strip() or None if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def cores() -> dict:
    """Cores this process may actually run on, and how many of them are fast.

    `os.cpu_count()` is the wrong number twice over: it ignores CPU affinity and
    cgroup quotas, and on a big.LITTLE machine it counts cores that are not
    interchangeable. This laptop is an M1 Pro -- 8 performance plus 2
    efficiency -- and a generation finishes when its SLOWEST worker finishes, so
    two trials parked on efficiency cores set the wall clock for all ten.
    """
    total = os.cpu_count() or 1
    usable, basis = total, "os.cpu_count()"

    if hasattr(os, "sched_getaffinity"):          # Linux
        usable = len(os.sched_getaffinity(0))
        basis = "sched_getaffinity"

    # cgroup v2 CPU quota, which is what a container actually gets.
    quota_path = Path("/sys/fs/cgroup/cpu.max")
    quota = None
    if quota_path.exists():
        try:
            spec, period = quota_path.read_text().split()[:2]
            if spec != "max":
                quota = max(1, int(float(spec) / float(period)))
                if quota < usable:
                    usable, basis = quota, "cgroup v2 cpu.max"
        except (OSError, ValueError):
            pass

    fast = None
    p0 = _sysctl("hw.perflevel0.logicalcpu")      # Darwin: performance tier
    p1 = _sysctl("hw.perflevel1.logicalcpu")      # Darwin: efficiency tier
    if p0:
        fast = int(p0)
    return {
        "total": total,
        "usable": usable,
        "usable_basis": basis,
        "performance": fast,
        "efficiency": int(p1) if p1 else None,
        "cgroup_quota": quota,
        "brand": _sysctl("machdep.cpu.brand_string") or platform.processor() or "unknown",
        # The number to parallelise over. Efficiency cores are not a free lane:
        # the generation waits on them.
        "schedulable": fast or usable,
    }


def memory() -> dict:
    """Memory a run may use *without swapping*.

    Swap is deliberately not counted as capacity. A trial that fits only because
    the kernel is paging does finish, eventually, and the engine's wall-clock
    budget is what it trips over first. This machine is sitting at 7.0 of 8.2 GiB
    of swap already, which is the state in which the suite was killed.
    """
    total = available = None
    basis = "unknown"

    meminfo = Path("/proc/meminfo")
    if meminfo.exists():                           # Linux
        vals = {}
        for line in meminfo.read_text().splitlines():
            k, _, rest = line.partition(":")
            m = re.match(r"\s*(\d+)", rest)
            if m:
                vals[k] = int(m.group(1)) * 1024
        total = vals.get("MemTotal")
        available = vals.get("MemAvailable")
        basis = "/proc/meminfo MemAvailable"
    else:                                          # Darwin
        ms = _sysctl("hw.memsize")
        total = int(ms) if ms else None
        try:
            vm = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5)
            page = int(re.search(r"page size of (\d+)", vm.stdout).group(1))
            def pages(label):
                m = re.search(rf"{label}:\s+(\d+)", vm.stdout)
                return int(m.group(1)) if m else 0
            # Free plus what the kernel can hand over without going to swap.
            available = (pages("Pages free") + pages("Pages inactive")
                         + pages("Pages purgeable")) * page
            basis = "vm_stat free+inactive+purgeable"
        except (OSError, subprocess.SubprocessError, AttributeError, ValueError):
            pass

    # A cgroup limit beats the host's own numbers: it is the ceiling that kills.
    cgroup_limit = None
    for p in (Path("/sys/fs/cgroup/memory.max"),
              Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")):
        if p.exists():
            try:
                raw = p.read_text().strip()
                if raw not in ("max", ""):
                    cgroup_limit = int(raw)
            except (OSError, ValueError):
                pass
            break
    if cgroup_limit and total and cgroup_limit < total:
        total = cgroup_limit
        if available is None or available > cgroup_limit:
            available = cgroup_limit
        basis += " (capped by cgroup memory.max)"

    swap_total = swap_used = None
    su = _sysctl("vm.swapusage")
    if su:
        def gib(label):
            m = re.search(rf"{label} = ([\d.]+)M", su)
            return int(float(m.group(1)) * 1048576) if m else None
        swap_total, swap_used = gib("total"), gib("used")
    elif meminfo.exists():
        vals = {}
        for line in meminfo.read_text().splitlines():
            k, _, rest = line.partition(":")
            m = re.match(r"\s*(\d+)", rest)
            if m:
                vals[k] = int(m.group(1)) * 1024
        swap_total = vals.get("SwapTotal")
        if swap_total is not None and vals.get("SwapFree") is not None:
            swap_used = swap_total - vals["SwapFree"]

    return {"total": total, "available": available, "available_basis": basis,
            "cgroup_limit": cgroup_limit,
            "swap_total": swap_total, "swap_used": swap_used}


def disk(path: Path | None = None) -> dict:
    u = shutil.disk_usage(path or REPO_ROOT)
    return {"total": u.total, "free": u.free, "used_pct": 100.0 * u.used / u.total}


def limits() -> dict:
    out = {}
    for name in ("RLIMIT_NOFILE", "RLIMIT_NPROC", "RLIMIT_AS"):
        const = getattr(resource, name, None)
        if const is None:
            continue
        soft, hard = resource.getrlimit(const)
        out[name] = {"soft": soft, "hard": hard,
                     "unlimited": soft == resource.RLIM_INFINITY}
    return out


def machine_id() -> str:
    """Stable per-machine key for stored measurements.

    Model, core count, total RAM, platform and interpreter minor version. A
    measurement is only transferable between runs of the same machine, and a
    different box must measure for itself rather than inherit a number.
    """
    c, m = cores(), memory()
    parts = [platform.system(), platform.machine(), c["brand"], str(c["total"]),
             str(m["total"]), ".".join(platform.python_version_tuple()[:2])]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# measuring a trial, rather than assuming one
# --------------------------------------------------------------------------- #
def load_store() -> dict:
    if STORE.exists():
        try:
            return json.loads(STORE.read_text())
        except (OSError, ValueError):
            return {}
    return {}


def measure_trial(profile: str, *, quiet: bool = False) -> dict:
    """Run ONE real trial at `purpose=check` and record its peak RSS.

    `purpose=check` is excluded from N by `runlog.trial_rows`, which filters on
    `purpose == "trial"`, so measuring costs no trial out of the registered
    budget. The run is a real one on real data on purpose: a synthetic profile
    walks a different number of ticks and would measure the wrong thing.
    """
    before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    t0 = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "run.py", "--profile", profile,
         "--purpose", "check", "--human", "--quiet"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    wall = time.monotonic() - t0
    after = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    if proc.returncode != 0:
        raise SystemExit(
            f"the measuring trial failed, so there is no footprint to record:\n"
            f"{(proc.stdout + proc.stderr)[-1500:]}"
        )
    # RUSAGE_CHILDREN reports the high-water mark over all children reaped so
    # far, so it is a max and not a sum -- `max(before, after)` is the peak of
    # the one child that peaked highest, which is what a per-trial budget wants.
    peak = max(before, after) * _RU_MAXRSS_TO_BYTES
    entry = {"profile": profile, "peak_rss_bytes": peak, "wall_s": wall,
             "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
             "python": platform.python_version(), "purpose": "check"}
    store = load_store()
    store.setdefault(machine_id(), {"machine": {
        "brand": cores()["brand"], "platform": platform.system(),
        "cores_total": cores()["total"], "memory_total": memory()["total"],
    }}).setdefault("profiles", {})[profile] = entry
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(store, indent=2, sort_keys=True) + "\n")
    if not quiet:
        print(f"  measured {profile}: peak RSS {peak/GIB:.2f} GiB over {wall:.0f}s "
              f"-> {STORE.relative_to(REPO_ROOT)}")
    return entry


def footprint(profile: str) -> dict | None:
    return (load_store().get(machine_id(), {}).get("profiles", {}) or {}).get(profile)


# --------------------------------------------------------------------------- #
# the planner
# --------------------------------------------------------------------------- #
def plan(width: int, profile: str, *, headroom: float = 0.85) -> dict:
    """Can this machine carry `width` concurrent trials of `profile`?

    `headroom` leaves a margin for the parent process, the proposing agent's
    HTTP work and the OS. Not tuned to make anything pass -- it is the one
    judgement call in here and it is stated rather than buried.
    """
    c, m, d = cores(), memory(), disk()
    fp = footprint(profile)
    out = {"width": width, "profile": profile, "machine": machine_id(),
           "cores": c, "memory": m, "disk": d, "limits": limits(),
           "footprint": fp, "headroom": headroom, "constraints": []}

    if fp is None:
        out["verdict"] = "UNKNOWN"
        out["reason"] = (
            f"no measured per-trial footprint for profile {profile!r} on this "
            f"machine ({machine_id()}). Run `scripts/capacity.py --measure "
            f"--profile {profile}` first; it spends one purpose=check trial, "
            f"which N does not count. Refusing to guess a footprint.")
        return out

    per = fp["peak_rss_bytes"]
    budget = (m["available"] or 0) * headroom
    mem_fits = int(budget // per) if per else width
    out["constraints"].append({
        "name": "memory", "fits": mem_fits,
        "detail": f"{per/GIB:.2f} GiB measured per trial, {(m['available'] or 0)/GIB:.2f} GiB "
                  f"available ({m['available_basis']}), {headroom:.0%} headroom"})
    out["constraints"].append({
        "name": "cores", "fits": c["schedulable"],
        "detail": (f"{c['performance']} performance cores of {c['total']} "
                   f"({c['efficiency']} efficiency, which a generation waits on)"
                   if c["performance"] else
                   f"{c['usable']} usable cores via {c['usable_basis']}")})
    # A worker tree is 0.9 MB and a trial's own artefacts ~7 KiB; the 60 MiB
    # nulls table is per campaign and not per worker. Disk has never been the
    # binding constraint here, and it is checked anyway because "never yet" is
    # not "cannot".
    per_worker_disk = 8 * 1048576
    out["constraints"].append({
        "name": "disk", "fits": int(d["free"] // per_worker_disk),
        "detail": f"{d['free']/GIB:.1f} GiB free ({d['used_pct']:.0f}% used), "
                  f"~8 MiB budgeted per worker tree"})
    nofile = out["limits"].get("RLIMIT_NOFILE", {})
    if nofile and not nofile.get("unlimited"):
        out["constraints"].append({
            "name": "open files", "fits": max(1, nofile["soft"] // 64),
            "detail": f"soft limit {nofile['soft']}, ~64 descriptors budgeted per worker"})

    binding = min(out["constraints"], key=lambda k: k["fits"])
    out["binding"] = binding["name"]
    out["max_width"] = max(1, binding["fits"])
    out["verdict"] = "OK" if width <= out["max_width"] else "REFUSE"
    out["reason"] = (
        f"{binding['name']} allows {binding['fits']} concurrent trial(s) "
        f"({binding['detail']})")

    if m["swap_total"] and m["swap_used"] is not None:
        frac = m["swap_used"] / m["swap_total"]
        if frac > 0.5:
            out["warning"] = (
                f"swap is {frac:.0%} used ({m['swap_used']/GIB:.1f} of "
                f"{m['swap_total']/GIB:.1f} GiB) before the run starts. The "
                f"memory figure above is physical headroom and does not count "
                f"swap, but a machine already paging this hard has other "
                f"processes that will want it back.")
    return out


def decide(profile: str, *, ceiling: int | None = None,
           measure_if_missing: bool = True, quiet: bool = False) -> dict:
    """Choose W for a pre-registration, and show the work.

    **Why this is allowed to choose, when the run stage is not.** W decides what
    each agent sees, so it has to be fixed before any trial is spent -- not
    because a machine must not pick it, but because it must not be re-picked
    once numbers exist. Registration is the moment where both are true: no trial
    has run, no score exists, and whatever is chosen here is written into the
    pre-registration and frozen there with everything else. A W re-derived at
    run time would be adaptation; a W derived at registration is a declaration
    that happens to have been computed rather than typed.

    So `--generation-width auto` is answered HERE, and `--stage run` keeps
    refusing: if capacity fell between registering and running, the right
    outcome is still a human deciding, because the declared number is now part
    of the record.

    Measures the machine if it has never been measured for this profile. That is
    the conditioning step, it costs one `purpose=check` trial, and N does not
    count it.
    """
    fp = footprint(profile)
    measured_now = False
    if fp is None and measure_if_missing:
        if not quiet:
            print(f"  no footprint for {profile!r} on this machine yet; "
                  f"measuring one purpose=check trial (outside N)", flush=True)
        measure_trial(profile, quiet=quiet)
        measured_now = True

    p = plan(1, profile)
    if p["verdict"] == "UNKNOWN":
        return {"width": None, "verdict": "UNKNOWN", "reason": p["reason"],
                "plan": p, "measured_now": measured_now}

    width = p["max_width"]
    limiter = p["binding"]
    if ceiling is not None and ceiling < width:
        width, limiter = ceiling, f"ceiling ({ceiling}) set by the caller"
    width = max(1, width)

    # Re-planned at the width actually chosen, so the recorded plan and the
    # decision describe the same run rather than the probe's W=1.
    p = plan(width, profile)
    out = {"width": width, "verdict": "OK", "plan": p, "binding": limiter,
           "max_width": p["max_width"], "ceiling": ceiling,
           "measured_now": measured_now, "machine": p["machine"],
           "profile": profile,
           "footprint_bytes": (p["footprint"] or {}).get("peak_rss_bytes"),
           "decided_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
           "reason": f"W={width}, bound by {limiter}"}

    # Isolation has only ever been PROVEN at four workers (worker_tree.py
    # --prove, 2026-10-04, 4/4 bit-identical to the sequential score). A wider
    # run is not forbidden -- `stage_run` calls `worker_tree.verify(width)`
    # every time -- but the record should say that the width was never put
    # through the equivalence proof rather than quietly implying it was.
    if width > PROVEN_WIDTH:
        out["unproven_width"] = (
            f"W={width} exceeds the widest isolation proof on record "
            f"(W={PROVEN_WIDTH}). `scripts/worker_tree.py --prove --create "
            f"{width}` spends only purpose=check runs and would close that gap.")
    if p.get("warning"):
        out["warning"] = p["warning"]
    return out


def render(p: dict) -> None:
    c, m, d = p["cores"], p["memory"], p["disk"]
    print(f"machine {p['machine']}  {c['brand']}")
    print(f"  cores     {c['total']} total", end="")
    if c["performance"]:
        print(f", {c['performance']} performance + {c['efficiency']} efficiency", end="")
    print(f"  -> {c['schedulable']} schedulable ({c['usable_basis']})")
    print(f"  memory    {(m['total'] or 0)/GIB:.1f} GiB total, "
          f"{(m['available'] or 0)/GIB:.1f} GiB available ({m['available_basis']})")
    if m["swap_total"]:
        print(f"  swap      {(m['swap_used'] or 0)/GIB:.1f} of "
              f"{m['swap_total']/GIB:.1f} GiB used")
    print(f"  disk      {d['free']/GIB:.1f} GiB free ({d['used_pct']:.0f}% used)")
    fp = p["footprint"]
    print(f"  trial     " + (
        f"{fp['peak_rss_bytes']/GIB:.2f} GiB peak RSS, {fp['wall_s']:.0f}s "
        f"(measured {fp['measured_at'][:10]}, profile {fp['profile']})"
        if fp else f"NOT MEASURED for profile {p['profile']!r}"))
    if p["verdict"] == "UNKNOWN":
        print(f"\n  UNKNOWN: {p['reason']}")
        return
    print()
    for k in p["constraints"]:
        mark = "<-- binding" if k["name"] == p["binding"] else ""
        print(f"    {k['name']:12} allows {k['fits']:>3}   {k['detail']} {mark}")
    print(f"\n  W={p['width']}: {p['verdict']}   (this machine carries W<={p['max_width']}, "
          f"bound by {p['binding']})")
    if p.get("warning"):
        print(f"\n  WARNING: {p['warning']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--profile", default="nq-train",
                    help="run profile whose per-trial footprint to use")
    ap.add_argument("--probe", action="store_true",
                    help="report cores, memory, disk and limits")
    ap.add_argument("--measure", action="store_true",
                    help="run one purpose=check trial and record its peak RSS")
    ap.add_argument("--width", type=int,
                    help="declared generation_width to check; exits 1 if refused")
    ap.add_argument("--recommend", action="store_true",
                    help="the largest W this machine carries, to DECLARE next time")
    ap.add_argument("--decide", action="store_true",
                    help="choose W for a pre-registration, measuring first if "
                         "this machine has never been measured for the profile")
    ap.add_argument("--ceiling", type=int, default=None,
                    help="never choose a W above this, whatever the machine says")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if a.measure:
        measure_trial(a.profile)

    if a.decide:
        d = decide(a.profile, ceiling=a.ceiling)
        if a.json:
            print(json.dumps(d, indent=2, sort_keys=True))
            return 0 if d["verdict"] == "OK" else 2
        if d["verdict"] != "OK":
            print(d["reason"], file=sys.stderr)
            return 2
        render(d["plan"])
        print(f"\n  DECIDED W={d['width']}  ({d['reason']})")
        if d.get("unproven_width"):
            print(f"  NOTE: {d['unproven_width']}")
        return 0

    p = plan(a.width or 1, a.profile)
    if a.json:
        print(json.dumps(p, indent=2, sort_keys=True))
    elif a.recommend:
        if p["verdict"] == "UNKNOWN":
            print(p["reason"])
            return 2
        print(f"{p['max_width']}")
    elif a.probe or a.width or a.measure:
        render(p)
    else:
        render(p)

    if a.width:
        sys.stdout.flush()
        if p["verdict"] == "REFUSE":
            print(f"\nRefusing W={a.width}. Not lowering it: generation_width is "
                  f"pre-registered and changes what each agent sees, so a "
                  f"narrower run is a different experiment. Declare W<="
                  f"{p['max_width']}, or free capacity.", file=sys.stderr)
            return 1
        if p["verdict"] == "UNKNOWN":
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
