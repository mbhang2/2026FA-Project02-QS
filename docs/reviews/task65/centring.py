"""Task 65 — is the conditional null centred where the sign-flip one is not?

Pre-registration: `docs/reviews/task65/README.md`, committed at 1576feb before
this ran. Paired: the same candidates, the same data, the same metric, two nulls.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import pathlib
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[3]
HERE = pathlib.Path(__file__).resolve().parent
SCRATCH = pathlib.Path(os.environ.get("CLAUDE_JOB_DIR", "/tmp")) / "tmp" / "task65"

CANDIDATES = 100
R = 150
B_SIGNFLIP = 10_000
ROT_SEED_BASE = 800_000
SOURCE_SEED = 64_001
DATASET_SEED = 4_242
WORKERS = 8


def _dataset(rot_seed=None):
    from src.core.data.synthetic import SyntheticDataset, SyntheticSpec
    from src.core.scoring.randomization.rotation import RotatedDataset
    ds = SyntheticDataset(SyntheticSpec(
        n_sessions=175, ticks_per_session=390, vol_per_tick=0.15,
        seed=DATASET_SEED, signal_strength=0.0))
    return ds if rot_seed is None else RotatedDataset(ds, seed=rot_seed)


def _score_one(path, rot_seed=None):
    from src.core.engine import Engine
    from src.core.engine.remote import RemoteStrategy
    from src.core.runlog import RunTicket
    from src.core.scoring.metrics import IDLE_CHARGE
    from src.core.scoring.scorer import score
    from src.core.types import Split

    ticket = RunTicket.for_null_calibration("task 65 centring")
    with RemoteStrategy(pathlib.Path(path), "Strategy") as strategy:
        engine = Engine(_dataset(rot_seed), strategy, ticket=ticket,
                        split=Split.TRAIN, warmup_sessions=0,
                        fill_model="unfavorable", seed=1)
        engine.run()
    blotter, calendar = engine.reconstructed(), engine.session_calendar()
    s = score(blotter, period=calendar, idle_charge=IDLE_CHARGE)
    return (None if s.rejected else float(s.primary)), blotter, calendar


def one(args):
    """One candidate: its observed score, its rotation null, its sign-flip null."""
    i, path = args
    sys.path.insert(0, str(REPO))
    from src.core.scoring.randomization import round_turn
    from src.core.scoring.metrics import IDLE_CHARGE

    observed, blotter, calendar = _score_one(path)
    if observed is None:
        return {"candidate": i, "observed": None}

    rot = []
    for r in range(R):
        s, _, _ = _score_one(path, rot_seed=ROT_SEED_BASE + i * R + r)
        if s is not None:
            rot.append(s)

    flip = round_turn.null_sample(blotter, calendar, b=B_SIGNFLIP, seed=17,
                                  idle_charge=IDLE_CHARGE)
    import numpy as np
    return {"candidate": i, "observed": observed,
            "rot_mean": float(np.mean(rot)) if rot else None,
            "rot_std": float(np.std(rot)) if rot else None,
            "rot_n": len(rot),
            "flip_mean": float(np.mean(flip)) if len(flip) else None,
            "flip_std": float(np.std(flip)) if len(flip) else None}


def main() -> None:
    sys.path.insert(0, str(REPO))
    from src.core.loop.agents import NullGenerator
    import numpy as np

    SCRATCH.mkdir(parents=True, exist_ok=True)
    src = SCRATCH / "sources"; src.mkdir(exist_ok=True)
    agent = NullGenerator(seed=SOURCE_SEED)
    paths = []
    for i in range(CANDIDATES):
        f = src / f"c{i}.py"
        f.write_text(agent.propose([]))
        paths.append((i, str(f)))

    print(f"{CANDIDATES} candidates x (1 + {R}) runs at {WORKERS} workers")
    t0 = time.monotonic()
    with mp.Pool(WORKERS) as pool:
        rows = pool.map(one, paths)
    print(f"done in {(time.monotonic()-t0)/60:.1f} min")

    ok = [r for r in rows if r.get("observed") is not None
          and r.get("rot_mean") is not None and r.get("flip_mean") is not None]
    obs = np.array([r["observed"] for r in ok])
    rot = np.array([r["rot_mean"] for r in ok])
    flip = np.array([r["flip_mean"] for r in ok])

    out = {
        "candidates_usable": len(ok),
        "candidates_total": CANDIDATES,
        "R": R,
        "rotation": {
            "fraction_below": float((obs < rot).mean()),
            "mean_gap": float((obs - rot).mean()),
            "dispersion_ratio": float(obs.std() / np.mean([r["rot_std"] for r in ok])),
        },
        "sign_flip": {
            "fraction_below": float((obs < flip).mean()),
            "mean_gap": float((obs - flip).mean()),
            "dispersion_ratio": float(obs.std() / np.mean([r["flip_std"] for r in ok])),
        },
    }
    (HERE / "centring.json").write_text(json.dumps(out, indent=2) + "\n")
    (SCRATCH / "rows.json").write_text(json.dumps(rows))

    print(f"\nusable candidates {len(ok)}/{CANDIDATES}")
    for name in ("rotation", "sign_flip"):
        a = out[name]
        print(f"  {name:10}  fraction_below {a['fraction_below']:.3f}   "
              f"mean_gap {a['mean_gap']:+.4f}   dispersion {a['dispersion_ratio']:.2f}")
    print("\ncentred is fraction_below ~0.50 and mean_gap ~0")
    print(f"wrote {HERE / 'centring.json'}")


if __name__ == "__main__":
    main()
