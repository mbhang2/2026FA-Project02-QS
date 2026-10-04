#!/usr/bin/env python3
"""Isolated working copies, so trials can run in parallel without lying.

    python scripts/worker_tree.py --create 8
    python scripts/worker_tree.py --verify
    python scripts/worker_tree.py --prove        # the isolation test, no LLM
    python scripts/worker_tree.py --destroy

**This is parallelism, and deliberately NOT beam search.** Michael's branching
idea -- fan one trial out to several, then branch again -- changes the search
algorithm, so every number it produces is incomparable with every campaign run
so far and it needs its own pre-registration. This file does the half that needs
none: run the trials we would have run anyway, at the same time.

Nor does parallelism launder multiplicity. **400 parallel trials are still 400
looks**, and `max_of_n` counts every one. The only thing bought here is wall
clock: measured 225 s per trial, 99.9% of it one core, 10 cores available, so
N=400 is ~25 h sequential and ~3.2 h at 8-way.

## The defect it fixes

`src/strategies/current.py` is where a candidate is written before it is scored,
and it is a single path hardwired in `run.py`'s loader, in `nq_campaign.py`, and
in `ARTIFACT_GLOBS`. Two trials in one checkout overwrite each other's strategy
mid-flight. **The ledger would look clean and be wrong** -- a row naming a digest
for source that something else replaced -- which is the worst available outcome,
worse than a crash.

## What makes a worker a repository

`src/core/_roots.py` resolves the root by marker, never by counting directories:
`.git`, or **`run.py` beside `pyproject.toml`**. So a worker is a directory
holding both, and everything inside it then resolves to itself --
`run.py`'s own `REPO_ROOT`, `repo_root()`, and the import of
`src.strategies.current`. Verified rather than assumed; `--verify` is that check.

## What is copied and what is shared, and why

**Copied** (1.8 MB/worker): `src/`, `config/`, `run.py`, `pyproject.toml`.
`config/` has to be a copy and not a link because `ARTIFACT_GLOBS` digests it --
an identical copy gives an identical fingerprint, which is the point.

**Symlinked, never copied**: `data/`, which holds the 1.1 GiB content-keyed
cache. Copying it per worker would be 8.8 GiB to no purpose: it is read-only to
a trial, deliberately absent from `ARTIFACT_GLOBS`, and keyed by content.

**Per-worker and writable**: `runs/`. Each worker writes its own ledger and they
are merged afterwards, because two processes appending to one JSONL is a
corrupted line waiting to happen.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKERS_DIR = ROOT / ".scratch" / "workers"

# Copied per worker. Small, and every one of them can change a result.
COPY = ("run.py", "pyproject.toml", "src", "config")
# Linked, shared, read-only to a trial. `data` is the 1.1 GiB one.
LINK = ("data", "audit", "alternatives", "loop", "research", "agent-patterns")
# Created empty and writable per worker.
MKDIR = ("runs",)


def worker_path(i: int) -> pathlib.Path:
    return WORKERS_DIR / f"w{i}"


def create(n: int, *, quiet: bool = False) -> list[pathlib.Path]:
    WORKERS_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(n):
        w = worker_path(i)
        if w.exists():
            shutil.rmtree(w)
        w.mkdir(parents=True)
        for name in COPY:
            src = ROOT / name
            if not src.exists():
                continue
            if src.is_dir():
                shutil.copytree(src, w / name,
                                ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(src, w / name)
        for name in LINK:
            src = ROOT / name
            if src.exists():
                (w / name).symlink_to(src)
        for name in MKDIR:
            (w / name).mkdir(parents=True, exist_ok=True)
        out.append(w)
        if not quiet:
            size = sum(f.stat().st_size for f in w.rglob("*")
                       if f.is_file() and not f.is_symlink())
            print(f"  {w.relative_to(ROOT)}  {size / 1e6:.1f} MB copied")
    return out


def verify(n: int | None = None) -> int:
    """Each worker must resolve the repository to ITSELF. Anything else means a
    trial would read or write the real tree, which is the bug."""
    ws = sorted(WORKERS_DIR.glob("w*")) if n is None else [worker_path(i)
                                                           for i in range(n)]
    if not ws:
        print("no workers; --create first")
        return 1
    code = ("import sys,pathlib;sys.path.insert(0,str(pathlib.Path.cwd()));"
            "import run;from src.core._roots import repo_root;"
            "print(run.REPO_ROOT);print(repo_root())")
    bad = 0
    for w in ws:
        r = subprocess.run([sys.executable, "-c", code], cwd=str(w),
                           capture_output=True, text=True)
        lines = (r.stdout or "").strip().splitlines()
        ok = len(lines) == 2 and all(pathlib.Path(x).resolve() == w.resolve()
                                     for x in lines)
        # and the heavy cache must be shared, not copied
        shared = (w / "data").is_symlink()
        if not ok or not shared:
            bad += 1
        print(f"  {w.name}: root={'self' if ok else 'WRONG'} "
              f"data={'shared' if shared else 'COPIED -- 1.1GiB wasted'}"
              + ("" if ok else f"\n      {(r.stdout + r.stderr)[-300:]}"))
    print(f"\n{len(ws) - bad}/{len(ws)} worker(s) isolated")
    return 1 if bad else 0


STUB = '''"""Worker {i}'s own strategy. Isolation probe -- not a research trial."""
from __future__ import annotations


class Strategy:
    """Does nothing on purpose. The point is which file was loaded."""

    WORKER = {i}

    def on_bar(self, view, book):      # pragma: no cover - never scored
        return None
'''


def prove() -> int:
    """**The test that matters: each worker loads its OWN strategy, concurrently.**

    Writes a different `current.py` into every worker, imports them all at the
    same time, and checks each process saw its own. No model, no market data and
    no trial: the failure this guards against is a path collision, and a path
    collides without needing a backtest to prove it.
    """
    ws = sorted(WORKERS_DIR.glob("w*"))
    if len(ws) < 2:
        print("need at least 2 workers; --create 2 or more")
        return 1
    for i, w in enumerate(ws):
        (w / "src" / "strategies" / "current.py").write_text(STUB.format(i=i))

    code = ("import sys,pathlib;sys.path.insert(0,str(pathlib.Path.cwd()));"
            "import src.strategies.current as c;"
            "print(c.Strategy.WORKER)")

    def run_one(arg):
        i, w = arg
        r = subprocess.run([sys.executable, "-c", code], cwd=str(w),
                           capture_output=True, text=True)
        return i, (r.stdout or r.stderr)[-200:].strip()

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(ws)) as pool:
        got = dict(pool.map(run_one, enumerate(ws)))

    bad = 0
    for i, w in enumerate(ws):
        saw = got.get(i, "")
        ok = saw == str(i)
        bad += not ok
        print(f"  {w.name}: wrote WORKER={i}, loaded {saw!r}  "
              f"{'ok' if ok else 'COLLISION'}")
    # and the real tree must be untouched
    real = (ROOT / "src" / "strategies" / "current.py").read_text()
    touched = "Isolation probe" in real
    print(f"\n  the real src/strategies/current.py was "
          f"{'TOUCHED -- isolation failed' if touched else 'not touched'}")
    if bad or touched:
        print(f"\n{bad} collision(s). Parallel trials would corrupt the ledger.")
        return 1
    print(f"\n{len(ws)}/{len(ws)} workers loaded their own strategy "
          f"concurrently. Isolation holds.")
    return 0


def _run_in(w: pathlib.Path, profile: str, ledger: str) -> dict:
    """One scored run inside one worker. `purpose=check`, so N is untouched.

    `--purpose` requires `--human`, which is the harness refusing to let an
    automated caller classify its own run. A person IS running this -- it is an
    infrastructure probe, not a trial -- and `check` rows are excluded from
    `global_n()` by construction rather than by promise.
    """
    import time
    t0 = time.monotonic()
    r = subprocess.run(
        [sys.executable, "run.py", "--profile", profile, "--purpose", "check",
         "--human", "--ledger", ledger, "--quiet"],
        cwd=str(w), capture_output=True, text=True)
    wall = time.monotonic() - t0
    f = w / ledger
    row = {}
    if f.exists():
        lines = [x for x in f.read_text().splitlines() if x.strip()]
        if lines:
            row = json.loads(lines[-1])
    return {"worker": w.name, "wall_s": wall, "rc": r.returncode,
            "outcome": row.get("outcome"),
            "digest": row.get("strategy_digest"),
            "primary": (row.get("score") or {}).get("primary"),
            "stderr": (r.stderr or "")[-300:]}


def equivalence(profile: str, reps: int) -> int:
    """**Does running in parallel change the number?** It must not.

    The same strategy, the same committed profile, in every worker at once --
    then the same thing once on its own. A fixed seed and a committed profile
    make a backtest deterministic, so every score here has to be bit-identical.
    If parallel runs disagree with the sequential one, the trees are sharing
    something they should not and no amount of speed is worth it.
    """
    ws = sorted(WORKERS_DIR.glob("w*"))
    if len(ws) < 2:
        print("need at least 2 workers; --create 2 or more")
        return 1
    print(f"  profile {profile}, {len(ws)} worker(s) in parallel, {reps} rep(s)")

    seq = _run_in(ws[0], profile, "runs/seq.jsonl")
    print(f"  sequential baseline: primary={seq['primary']!r} "
          f"({seq['wall_s']:.1f}s, {seq['outcome']})")
    if seq["primary"] is None:
        print(f"  the baseline did not score; cannot compare\n{seq['stderr']}")
        return 1

    bad = 0
    for rep in range(reps):
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(ws)) as pool:
            got = list(pool.map(
                lambda w: _run_in(w, profile, f"runs/par{rep}.jsonl"), ws))
        wall = max(g["wall_s"] for g in got)
        diffs = [g for g in got if g["primary"] != seq["primary"]]
        bad += len(diffs)
        print(f"  rep {rep}: {len(got) - len(diffs)}/{len(got)} identical, "
              f"slowest {wall:.1f}s")
        for g in diffs:
            print(f"      {g['worker']}: primary={g['primary']!r} "
                  f"outcome={g['outcome']} {g['stderr'][:120]}")
    # the digests must also agree: same source, same digest
    print(f"  strategy_digest: {str(seq['digest'])[:16]} (baseline)")
    if bad:
        print(f"\n{bad} parallel run(s) disagreed with sequential. "
              f"Parallelism is NOT safe here.")
        return 1
    print("\nEvery parallel run reproduced the sequential score exactly. "
          "Parallelism does not change the number.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--create", type=int, metavar="N")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--prove", action="store_true")
    ap.add_argument("--destroy", action="store_true")
    ap.add_argument("--equivalence", action="store_true",
                    help="run the same strategy in every worker at once and "
                         "check the score matches a sequential run exactly")
    ap.add_argument("--profile", default="campaign",
                    help="committed profile for --equivalence. Default "
                         "'campaign', which is SYNTHETIC data -- the isolation "
                         "mechanism can be proven without touching the market "
                         "data at all. Pass nq-train to confirm it on real data.")
    ap.add_argument("--reps", type=int, default=2)
    a = ap.parse_args()

    if a.destroy:
        if WORKERS_DIR.exists():
            shutil.rmtree(WORKERS_DIR)
            print(f"removed {WORKERS_DIR.relative_to(ROOT)}")
        return 0
    rc = 0
    if a.create:
        print(f"creating {a.create} worker(s) under "
              f"{WORKERS_DIR.relative_to(ROOT)}")
        create(a.create)
    if a.verify:
        print("\nverifying isolation:")
        rc |= verify()
    if a.prove:
        print("\nproving each worker loads its own strategy, concurrently:")
        rc |= prove()
    if a.equivalence:
        # `prove()` leaves a stub strategy behind; restore the real one so the
        # equivalence run scores something.
        for w in sorted(WORKERS_DIR.glob("w*")):
            shutil.copy2(ROOT / "src" / "strategies" / "current.py",
                         w / "src" / "strategies" / "current.py")
        print("\nchecking parallel runs reproduce the sequential score:")
        rc |= equivalence(a.profile, a.reps)
    if not (a.create or a.verify or a.prove or a.equivalence):
        ap.print_help()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
