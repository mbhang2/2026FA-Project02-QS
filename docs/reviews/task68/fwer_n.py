"""Task 68 — FWER of rotation + max_of_n at N = 10 and N = 20.

Task 66's harness with N parameterised and nothing else touched: same
generator seed, same dataset spec and per-campaign seeds, same rotation seed
block, same correction, same accept rule, same in-process substitution. A
difference between legs is then a property of N.

ORIGINAL HEADER, which still describes the machinery:
Task 66 — FWER of rotation + max_of_n at N = 50.

Pre-registration: `docs/reviews/task66/README.md`, committed at cc57484.

Two things Task 65's harness got wrong and this one does not: it reports
progress (`pool.map` returns everything at once and says nothing in between), and
it uses `chunksize=1` so a long uneven tail does not leave workers idle.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import multiprocessing as mp
import os
import pathlib
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[3]
HERE = pathlib.Path(__file__).resolve().parent
SCRATCH = pathlib.Path(os.environ.get("CLAUDE_JOB_DIR", "/tmp")) / "tmp" / "task68"
# One file per leg: the legs are separate measurements that share a harness,
# and mixing them in one ledger would make resume ambiguous.
def raw_for(n): return SCRATCH / f"campaigns_n{n}.jsonl"
SRC = SCRATCH / "sources"

CAMPAIGNS = 100
N_TRIALS = 10        # overridden by --n
R = 150
R_SHORT = 50
Q = 0.05
B_MAXN = 10_000
MAXN_SEED = 17
SOURCE_SEED = 66_001
DATASET_SEED_BASE = 4_242
ROT_SEED_BASE = 800_000
WORKERS = 8


def _load(path, tag):
    spec = importlib.util.spec_from_file_location(f"cand_{tag}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Strategy


def _score(cls, spec, rot_seed=None):
    from src.core.data.synthetic import SyntheticDataset
    from src.core.engine import Engine
    from src.core.scoring.metrics import IDLE_CHARGE
    from src.core.scoring.randomization.rotation import RotatedDataset
    from src.core.scoring.scorer import score
    from src.core.types import Split

    ds = SyntheticDataset(spec)
    if rot_seed is not None:
        ds = RotatedDataset(ds, seed=rot_seed)
    engine = Engine.for_test(ds, cls(), split=Split.TRAIN, warmup_sessions=0,
                             fill_model="unfavorable", seed=1)
    engine.run()
    s = score(engine.reconstructed(), period=engine.session_calendar(),
              idle_charge=IDLE_CHARGE)
    return None if s.rejected else float(s.primary)


def _one_campaign(args):
    ci, paths = args
    sys.path.insert(0, str(REPO))
    from src.core.data.synthetic import SyntheticSpec
    from src.core.scoring import correction

    spec = SyntheticSpec(n_sessions=175, ticks_per_session=390, vol_per_tick=0.15,
                         seed=DATASET_SEED_BASE + ci, signal_strength=0.0)
    observed, nulls, unscorable = [], [], 0
    for ti, path in enumerate(paths):
        try:
            cls = _load(path, f"{ci}_{ti}")
            o = _score(cls, spec)
        except Exception:                                     # noqa: BLE001
            unscorable += 1
            continue
        if o is None:
            unscorable += 1
            continue
        base = ROT_SEED_BASE + (ci * N_TRIALS + ti) * R
        draws = []
        for r in range(R):
            try:
                s = _score(cls, spec, rot_seed=base + r)
            except Exception:                                 # noqa: BLE001
                continue
            if s is not None:
                draws.append(s)
        if not draws:
            unscorable += 1
            continue
        observed.append(o)
        nulls.append(draws)

    if not observed:
        return {"campaign": ci, "scored": 0, "unscorable": unscorable,
                "best": None, "rejected_150": False, "accepted_150": False,
                "rejected_50": False, "accepted_50": False}
    best = max(observed)
    # **R = 50 is a prefix of R = 150, so the second arm is free and paired.**
    # Same candidates, same rotations, same observed best -- only the null sample
    # size moves, which is the contrast `resampling_bias` claims to describe.
    out = {"campaign": ci, "scored": len(observed), "unscorable": unscorable,
           "best": best}
    for tag, r in (("150", R), ("50", R_SHORT)):
        v = correction.max_of_n([d[:r] for d in nulls], best,
                                b=B_MAXN, seed=MAXN_SEED, q=Q)
        out[f"p_{tag}"] = float(v["p"])
        out[f"rejected_{tag}"] = bool(v["reject"])
        out[f"accepted_{tag}"] = bool(v["reject"] and best > 0)
    return out


def sources(n: int) -> list[str]:
    sys.path.insert(0, str(REPO))
    from src.core.loop.agents import NullGenerator
    SRC.mkdir(parents=True, exist_ok=True)
    agent = NullGenerator(seed=SOURCE_SEED)
    out = []
    for i in range(n):
        f = SRC / f"c{i}.py"
        text = agent.propose([])
        if not f.exists():
            f.write_text(text)
        out.append(str(f))
    return out


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def batches(lo, hi):
    paths = sources(hi * N_TRIALS)
    for c in range(lo, hi):
        yield (c, paths[c * N_TRIALS:(c + 1) * N_TRIALS])


def stage_verify() -> None:
    """The substitution check: in-process against remote, blotter digests."""
    sys.path.insert(0, str(REPO))
    from src.core.data.synthetic import SyntheticDataset, SyntheticSpec
    from src.core.engine import Engine
    from src.core.engine.remote import RemoteStrategy
    from src.core.runlog import RunTicket
    from src.core.types import Split

    paths = sources(20)
    spec = SyntheticSpec(n_sessions=175, ticks_per_session=390, vol_per_tick=0.15,
                         seed=DATASET_SEED_BASE, signal_strength=0.0)
    same = 0
    for i, p in enumerate(paths):
        with RemoteStrategy(pathlib.Path(p), "Strategy") as s:
            e = Engine(SyntheticDataset(spec), s,
                       ticket=RunTicket.for_null_calibration("task66 verify"),
                       split=Split.TRAIN, warmup_sessions=0,
                       fill_model="unfavorable", seed=1)
            e.run()
        e2 = Engine.for_test(SyntheticDataset(spec), _load(p, f"v{i}")(),
                             split=Split.TRAIN, warmup_sessions=0,
                             fill_model="unfavorable", seed=1)
        e2.run()
        ok = e.blotter.digest() == e2.blotter.digest()
        same += ok
        if not ok:
            print(f"  MISMATCH on candidate {i}: the substitution is invalid")
    print(f"blotter digests identical on {same}/{len(paths)} candidates")
    if same != len(paths):
        sys.exit("in-process is not the same run. Do not use these results.")


def stage_run() -> None:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    done = set()
    if RAW.exists():
        done = {json.loads(x)["campaign"] for x in RAW.read_text().splitlines() if x.strip()}
        print(f"resuming: {len(done)} campaigns on disk")
    todo = [b for b in batches(0, CAMPAIGNS) if b[0] not in done]
    print(f"{len(todo)} campaigns x {N_TRIALS} trials x {R} rotations "
          f"= {len(todo)*N_TRIALS*(R+1):,} runs at {WORKERS} workers", flush=True)
    t0 = time.monotonic()
    with mp.Pool(WORKERS) as pool, RAW.open("a") as fh:
        for i, row in enumerate(pool.imap_unordered(_one_campaign, todo, chunksize=1), 1):
            fh.write(json.dumps(row) + "\n"); fh.flush()
            rate = i / (time.monotonic() - t0)
            print(f"  {i}/{len(todo)}  {rate*3600:.1f}/h  "
                  f"~{(len(todo)-i)/rate/3600:.1f}h left", flush=True)
    print(f"done in {(time.monotonic()-t0)/3600:.2f}h")


def stage_report() -> None:
    import json as _json
    series = []
    for n in (10, 20):
        f = raw_for(n)
        if not f.exists():
            print(f"  N={n}: not run")
            continue
        rows = [_json.loads(x) for x in f.read_text().splitlines() if x.strip()]
        c = len(rows)
        acc = sum(r.get("accepted_150", False) for r in rows)
        rej = sum(r.get("rejected_150", False) for r in rows)
        series.append({"N": n, "campaigns": c,
                       "accept_rate": acc / c, "accept_ci": wilson(acc, c),
                       "reject_rate": rej / c})
    # Task 66's N = 50, read from its committed result rather than retyped.
    t66 = HERE.parent / "task66" / "fwer.json"
    if t66.exists():
        d = _json.loads(t66.read_text())
        series.append({"N": 50, "campaigns": d["campaigns"],
                       "accept_rate": d["R150"]["accept_rate"],
                       "accept_ci": d["R150"]["accept_ci"],
                       "reject_rate": d["R150"]["reject_rate"]})
    out = {"null_source": "rotation", "correction": "max_of_n", "R": R,
           "q": Q, "idle_charge": 0.18, "series": series}
    lo = max(s["accept_ci"][0] for s in series) if series else 0
    hi = min(s["accept_ci"][1] for s in series) if series else 1
    out["all_intervals_overlap"] = bool(lo <= hi)
    (HERE / "fwer_by_n.json").write_text(_json.dumps(out, indent=2) + "\n")
    print(f"{'N':>4} {'campaigns':>10} {'accept':>8}  95% CI")
    for s in series:
        print(f"{s['N']:>4} {s['campaigns']:>10} {s['accept_rate']:>8.4f}  "
              f"[{s['accept_ci'][0]:.4f}, {s['accept_ci'][1]:.4f}]")
    print(f"\nall intervals overlap: {out['all_intervals_overlap']}")
    print(f"wrote {HERE/'fwer_by_n.json'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=("verify", "run", "report"))
    ap.add_argument("--n", type=int, choices=(10, 20), help="trials per campaign")
    a = ap.parse_args()
    if a.stage == "run":
        if a.n is None:
            sys.exit("--stage run needs --n 10 or --n 20")
        globals()["N_TRIALS"] = a.n
        globals()["RAW"] = raw_for(a.n)
    {"verify": stage_verify, "run": stage_run, "report": stage_report}[a.stage]()
