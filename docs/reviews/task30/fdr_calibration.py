#!/usr/bin/env python3
"""Does BH control at q on known-null campaigns? Measured over many of them.

    python docs/reviews/task30/fdr_calibration.py --campaigns 40 --trials 50

The 200-trial null campaign produced 2 BH discoveries at q=0.05, and one campaign
cannot tell what that means. Under the global null -- every candidate edgeless,
which is true here by construction -- BH's FDR reduces to the family-wise error
rate, so `P(any discovery) <= q`. A campaign with discoveries is therefore an
event that is *supposed* to happen about one time in twenty, and observing one is
not evidence of anything.

There is a second reason one campaign says little, and it is the more important
one: **the 200 trials share a single data realization.** Same synthetic seed,
same 105 sessions, same price path. They are not 200 independent looks at the
null; they are 200 correlated views of one draw from it. If that draw sits in the
tail, many strategies see it there together.

So the measurement has to vary the thing the campaign holds fixed. Each campaign
here gets **its own dataset seed**, and the question is what fraction of
campaigns produce at least one BH discovery. That fraction is the realized FWER
under the global null, and it is what `q` promises to bound.

In process rather than through `run.py`, because the subprocess loop is verified
separately and 2,000 trials through it is 45 minutes for no additional
information about the correction.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from src.core.data.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
from src.core.engine import Engine  # noqa: E402
from src.core.loop.agents import NullGenerator  # noqa: E402
from src.core.scoring.calibration import null as nullmod  # noqa: E402
from src.core.scoring.correction import correct  # noqa: E402
from src.core.scoring.scorer import score  # noqa: E402
from src.core.types import Split  # noqa: E402


def load_strategy(source: str, tag: str):
    import importlib.util

    path = pathlib.Path(REPO / "runs" / "tmp-calib")
    path.mkdir(parents=True, exist_ok=True)
    f = path / f"{tag}.py"
    f.write_text(source)
    spec = importlib.util.spec_from_file_location(f"calib_{tag}", f)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    f.unlink()
    return mod.Strategy


def one_campaign(dataset_seed: int, trials: int, agent_seed: int, q: float) -> dict:
    spec = SyntheticSpec(n_sessions=175, ticks_per_session=390,
                         vol_per_tick=0.15, seed=dataset_seed)
    agent = NullGenerator(seed=agent_seed)
    fars, scores = [], []
    for i in range(trials):
        source = agent.propose([])
        cls = load_strategy(source, f"s{dataset_seed}_{i}")
        engine = Engine.for_test(SyntheticDataset(spec), cls(),
                                 split=Split.TRAIN, warmup_sessions=0,
                                 fill_model="unfavorable", seed=1)
        engine.run()
        s = score(engine.reconstructed(), period=engine.session_calendar())
        if s.rejected:
            fars.append(1.0)
            continue
        fars.append(nullmod.far_for_score(s, dataset="synthetic")["far"])
        scores.append(s.primary)
    r = correct(fars, q=q)
    return {
        "dataset_seed": dataset_seed,
        "bh": r.bh_discoveries, "holm": r.holm_discoveries,
        "uncorrected": r.uncorrected_discoveries,
        "min_far": min(fars), "best_score": max(scores) if scores else None,
        "at_0.001": sum(1 for f in fars if f <= 0.001),
        "n": len(fars),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaigns", type=int, default=40)
    ap.add_argument("--trials", type=int, default=50)
    ap.add_argument("--q", type=float, default=0.05)
    args = ap.parse_args()

    t0 = time.monotonic()
    rows = []
    for c in range(args.campaigns):
        rows.append(one_campaign(1000 + c, args.trials, agent_seed=c, q=args.q))
        if (c + 1) % 5 == 0:
            hits = sum(1 for r in rows if r["bh"] > 0)
            print(f"  {c + 1}/{args.campaigns} campaigns, {hits} with a BH "
                  f"discovery, {time.monotonic() - t0:.0f}s", flush=True)

    n_camp = len(rows)
    total_trials = sum(r["n"] for r in rows)
    bh_any = sum(1 for r in rows if r["bh"] > 0)
    holm_any = sum(1 for r in rows if r["holm"] > 0)
    unc_any = sum(1 for r in rows if r["uncorrected"] > 0)
    at001 = sum(r["at_0.001"] for r in rows)

    print()
    print(f"{n_camp} campaigns x {args.trials} edgeless trials = "
          f"{total_trials:,} trials, q={args.q}")
    print(f"  campaigns with any BH discovery     {bh_any}/{n_camp} = "
          f"{bh_any / n_camp:.3f}   (q promises <= {args.q})")
    print(f"  campaigns with any Holm discovery   {holm_any}/{n_camp} = "
          f"{holm_any / n_camp:.3f}")
    print(f"  campaigns with any uncorrected hit  {unc_any}/{n_camp} = "
          f"{unc_any / n_camp:.3f}   <- what no correction looks like")
    print()
    print(f"  per-trial accepts at far <= 0.001   {at001}/{total_trials} = "
          f"{at001 / total_trials:.5f}   (promised 0.001)")
    print(f"  ratio measured/promised             "
          f"{at001 / total_trials / 0.001:.2f}x")
    print()
    verdict = ("BH CONTROLS" if bh_any / n_camp <= args.q
               else "BH DOES NOT CONTROL at the stated q")
    print(verdict)
    (pathlib.Path(__file__).parent / "fdr-calibration.json").write_text(
        json.dumps({"campaigns": n_camp, "trials_each": args.trials, "q": args.q,
                    "bh_any": bh_any, "holm_any": holm_any,
                    "uncorrected_any": unc_any,
                    "per_trial_at_0.001": at001 / total_trials,
                    "rows": rows}, indent=1, sort_keys=True))
    print(f"wall {time.monotonic() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
