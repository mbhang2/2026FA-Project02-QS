#!/usr/bin/env python3
"""Task 57: the FWER of `round_turn_mc` + `max_of_n`, measured.

    python docs/reviews/task57/fwer_round_turn_mc.py --stage verify
    python docs/reviews/task57/fwer_round_turn_mc.py --stage runs --arm a
    python docs/reviews/task57/fwer_round_turn_mc.py --stage runs --arm b
    python docs/reviews/task57/fwer_round_turn_mc.py --stage report

Pre-registered in this directory's README, committed before this file existed.
Nothing here is tuned in response to a result.

**Arm A is paired with Task 38.** `NullGenerator(seed=33_002)` indices 0-3999 are
Task 33's candidates, so the candidates, the data, N, q and the correction are all
identical and the ONLY difference is where each candidate's null comes from.
`--stage verify` checks that the regenerated scores reproduce before anything
rests on the pairing.

**Arm B is the configuration we operate**: C = 400 x N = 50.

Runs use `RunTicket.for_null_calibration`, so none of them is a trial and none
touches N.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys
import time
from concurrent.futures import ProcessPoolExecutor

REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
OUT = pathlib.Path(__file__).parent

# Task 33's constants, copied from `docs/reviews/task38/hybrid_fwer_r150.py`
# rather than retyped. Not parameters -- if any of these moves the comparison is
# between two different experiments, which the verify stage exists to catch and
# did: the first draft of this file guessed DATASET_SEED = 1 and every one of the
# seven verification scores differed.
N_SESSIONS, TICKS, VOL = 175, 390, 0.15
DATASET_SEED = 4_242
CANDIDATE_SEED = 33_002
B_NULL = 10_000
Q = 0.05
C_CAMPAIGNS = 400
ARMS = {"a": {"n": 10, "offset": 0}, "b": {"n": 50, "offset": 4_000}}
WORKERS = 9


def _run(path: str) -> dict:
    """One isolated run. Top level so it can cross a process pool.

    Returns the score and the candidate's round turns -- `(gross, cost, session)`
    triples, which is everything the sign-flip null reads. Blotters for 24,000
    candidates do not fit anywhere convenient; these do.
    """
    sys.path.insert(0, str(REPO))
    from src.core.data.synthetic import SyntheticDataset, SyntheticSpec
    from src.core.engine import Engine
    from src.core.engine.remote import RemoteStrategy
    from src.core.runlog import RunTicket
    from src.core.scoring.randomization.round_turn import round_turns
    from src.core.scoring.scorer import score
    from src.core.types import Split

    ds = SyntheticDataset(SyntheticSpec(
        n_sessions=N_SESSIONS, ticks_per_session=TICKS, vol_per_tick=VOL,
        seed=DATASET_SEED, signal_strength=0.0))
    ticket = RunTicket.for_null_calibration("task 57 FWER for round_turn_mc")
    with RemoteStrategy(pathlib.Path(path), "Strategy") as strategy:
        engine = Engine(ds, strategy, ticket=ticket, split=Split.TRAIN,
                        warmup_sessions=0, fill_model="unfavorable", seed=1)
        engine.run()
    blotter = engine.reconstructed()
    period = engine.session_calendar()
    s = score(blotter, period=period)
    turns = round_turns(blotter, period)
    return {
        "score": None if s.rejected else s.primary,
        "sessions": [c.session for c in period],
        "turns": [[t.gross, t.cost, t.session] for t in turns],
    }


def sources(lo: int, hi: int) -> list[str]:
    """Task 33's candidate stream. One stream, so 'fresh' cannot mean 'again'."""
    from src.core.loop.agents import NullGenerator

    gen = NullGenerator(seed=CANDIDATE_SEED)
    out = []
    for i in range(hi):
        src = gen.propose([])
        if i >= lo:
            out.append(src)
    return out


def _write_sources(srcs, tmp: pathlib.Path) -> list[str]:
    tmp.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, src in enumerate(srcs):
        p = tmp / f"cand_{i:06d}.py"
        if not p.exists():
            p.write_text(src)
        paths.append(str(p))
    return paths


def stage_verify() -> None:
    """The pairing is worth nothing if the regenerated sources differ."""
    base = json.loads((REPO / "docs/reviews/task33/campaigns.json").read_text())["base"]
    idx = [0, 1, 2, 500, 1500, 2500, 3999]
    srcs = sources(0, 4_000)
    tmp = OUT / "_sources"
    paths = _write_sources([srcs[i] for i in idx], tmp / "verify")
    print("verifying Task 33's scores reproduce", flush=True)
    with ProcessPoolExecutor(max_workers=min(WORKERS, len(idx))) as pool:
        got = list(pool.map(_run, paths))
    ok = True
    for i, g in zip(idx, got):
        want = base[i]["score"]
        same = (want is None and g["score"] is None) or (
            want is not None and g["score"] is not None
            and abs(want - g["score"]) < 1e-12)
        ok &= same
        print(f"  #{i:>4}  task33 {want}   now {g['score']}   {'OK' if same else 'DIFFERS'}")
    print("PAIRING VALID" if ok else "PAIRING BROKEN -- arm A is not comparable")
    if not ok:
        sys.exit(2)


def stage_runs(arm: str) -> None:
    spec = ARMS[arm]
    n_total = C_CAMPAIGNS * spec["n"]
    lo = spec["offset"]
    out = OUT / f"arm-{arm}-runs.json"
    if out.exists():
        print(f"arm {arm}: runs already present")
        return
    print(f"arm {arm}: {C_CAMPAIGNS} campaigns x N={spec['n']} = {n_total:,} runs",
          flush=True)
    srcs = sources(lo, lo + n_total)
    paths = _write_sources(srcs, OUT / "_sources" / f"arm{arm}")

    t0, done = time.monotonic(), []
    with ProcessPoolExecutor(max_workers=WORKERS) as pool:
        for i, r in enumerate(pool.map(_run, paths, chunksize=8), start=1):
            done.append(r)
            if i % 500 == 0:
                rate = i / (time.monotonic() - t0)
                print(f"  {i:,}/{n_total:,}  {rate:.1f}/s  eta "
                      f"{(n_total - i) / rate / 60:.0f} min", flush=True)
    out.write_text(json.dumps(done))
    print(f"arm {arm}: {n_total:,} runs in {(time.monotonic() - t0) / 60:.0f} min")


def _gate_one(campaign: list[dict]) -> dict:
    from src.core.scoring.correction import max_of_n
    from src.core.scoring.randomization.round_turn import RoundTurn, null_from_turns

    samples, scores = [], []
    for k, r in enumerate(campaign):
        if r["score"] is None:
            samples.append([])
            continue
        scores.append(r["score"])
        turns = [RoundTurn(gross=g, cost=c, session=s, n_fills=0)
                 for g, c, s in r["turns"]]
        samples.append(null_from_turns(turns, r["sessions"], b=B_NULL,
                                       seed=70_000 + k))
    if not scores:
        return {"accept": False, "reject": False, "p": 1.0, "best": None}
    best = max(scores)
    g = max_of_n(samples, best, b=B_NULL, q=Q)
    return {"accept": bool(g["reject"] and best > 0), "reject": bool(g["reject"]),
            "p": g["p"], "best": best}


def wilson(k: int, n: int) -> list[float]:
    """95% Wilson score interval, the same one Task 38 and Task 59 report.

    Not the Wald interval. At k = 0 the Wald half-width is `1.96 * sqrt(0/n)`,
    which is exactly 0, and a floor inside the sqrt to avoid that turns an
    undefined interval into a fabricated one -- an earlier draft of this file
    floored at 1e-12 and printed [0.000, 0.0000001], claiming five orders of
    magnitude more precision than 400 campaigns can buy. Wilson does not
    degenerate at the boundary: 0/400 gives an upper bound near 0.007, which is
    the honest statement of what "we saw none" resolves to here.
    """
    z = 1.96
    p, zn = k / n, z * z / n
    centre = (p + zn / 2) / (1 + zn)
    half = (z / (1 + zn)) * math.sqrt(p * (1 - p) / n + zn / (4 * n))
    return [max(0.0, centre - half), min(1.0, centre + half)]


def stage_report() -> None:
    res = {}
    for arm, spec in ARMS.items():
        f = OUT / f"arm-{arm}-runs.json"
        if not f.exists():
            print(f"arm {arm}: no runs yet"); continue
        runs = json.loads(f.read_text())
        n = spec["n"]
        campaigns = [runs[i * n:(i + 1) * n] for i in range(C_CAMPAIGNS)]
        # Parallel over campaigns: 400 campaigns x N nulls at B = 10,000 is
        # hours if it runs serially, and each campaign is independent.
        t0 = time.monotonic()
        verdicts = []
        with ProcessPoolExecutor(max_workers=WORKERS) as pool:
            for i, v in enumerate(pool.map(_gate_one, campaigns, chunksize=4),
                                  start=1):
                verdicts.append(v)
                if i % 50 == 0:
                    rate = i / (time.monotonic() - t0)
                    print(f"  arm {arm}: {i}/{C_CAMPAIGNS} gated  "
                          f"eta {(C_CAMPAIGNS - i) / rate / 60:.0f} min", flush=True)
        acc = sum(1 for v in verdicts if v["accept"])
        rej = sum(1 for v in verdicts if v["reject"])
        p = acc / len(verdicts)
        res[arm] = {
            "n_per_campaign": n, "campaigns": len(verdicts),
            "accepts": acc, "fwer": p,
            "ci95": wilson(acc, len(verdicts)),
            "reject_null_only": rej,
            "reject_rate_before_floor": rej / len(verdicts),
            "verdicts": verdicts,
        }
        print(f"\narm {arm}  N={n}  {acc}/{len(verdicts)} accept  "
              f"FWER {p:.4f}  CI [{res[arm]['ci95'][0]:.4f}, {res[arm]['ci95'][1]:.4f}]")
        print(f"        reject-the-null before the sortino>0 floor: {rej} "
              f"({rej / len(verdicts):.4f})")
    (OUT / "fwer.json").write_text(json.dumps(
        {"pre_registration": "docs/reviews/task57/README.md",
         "b_null": B_NULL, "q": Q, "campaigns": C_CAMPAIGNS,
         "task38_conditional_r150": {"fwer": 0.060, "ci": [0.041, 0.088], "n": 10},
         "arms": res}, indent=2))
    if "a" in res:
        a = res["a"]
        print(f"\nPAIRED COMPARISON (arm A, N=10)")
        print(f"  round_turn_mc   {a['fwer']:.4f}  CI [{a['ci95'][0]:.4f}, {a['ci95'][1]:.4f}]")
        print(f"  conditional     0.0600  CI [0.041, 0.088]   (Task 38, R=150)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=("verify", "runs", "report"))
    ap.add_argument("--arm", choices=tuple(ARMS))
    a = ap.parse_args()
    if a.stage == "verify":
        stage_verify()
    elif a.stage == "runs":
        if not a.arm:
            sys.exit("--stage runs needs --arm a|b")
        stage_runs(a.arm)
    else:
        stage_report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
