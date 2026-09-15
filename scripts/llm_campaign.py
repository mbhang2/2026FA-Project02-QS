#!/usr/bin/env python3
"""Task 36: the first campaign with a model proposing, and its control arm.

    python scripts/llm_campaign.py --stage register   # pre-register, hashed
    python scripts/llm_campaign.py --stage run        # both arms
    python scripts/llm_campaign.py --stage nulls      # route, then rotate
    python scripts/llm_campaign.py --stage report     # correct, then compare

Four stages because the run takes hours and a crash in hour three should not
cost hours one and two. `register` refuses to overwrite: the record is the
pre-registration and rewriting it is the failure this project exists to prevent.

**Two arms, identical but for one thing.** The primary arm is shown its own
prior trials with their scores. The control arm is shown nothing about its past
-- same model, same temperature, same documents, same budget, same profile. If
the trajectories do not differ, the loop is random search with a language model
attached, and that is worth establishing before anything is built on top of it.

**The control arm's proposals are generated in parallel and that is not a
shortcut.** An agent shown no history has no dependence on the order its
proposals are made in, so generating them concurrently is the same experiment
run faster. The primary arm cannot be parallelised and is not.

**Nothing here shows the agent the gate.** No threshold, no p-value, no
false-acceptance rate, no verdict, no N. The correction happens in `report`,
after every call has been made.

Training split only. Validation and holdout are untouched.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

OUT = REPO / "runs" / "campaigns" / "task36"
WORK = REPO / "runs" / "tmp-task36"

# ---- the pre-registration, as constants so it is readable in one place ---- #
PROFILE = "campaign"                 # synthetic, train, unfavorable, seed 1
BUDGET = 100                         # trials per arm
Q = 0.05
MODEL = "claude-haiku-4-5"
TEMPERATURE = 0.7
MAX_TOKENS = 16_384                  # measured: this model reasons ~5.7k tokens
                                     # before answering and truncates at 8k
MAX_REPAIR_ATTEMPTS = 2
REQUEST_TIMEOUT_S = 240.0
PROPOSAL_WALL_CLOCK_S = 900.0
FALLBACK_R = 150                     # Task 34's adopted remedy; below it the
                                     # correction's own resampling bias is
                                     # larger than the effect being measured
DATASET_SEED = 1                     # the campaign profile's seed
N_SESSIONS, TICKS, VOL = 175, 390, 0.15
B_PERM = 10_000
WORKERS = 9

RUN_FACTS = """- dataset: a **simulated** instrument, not NQ. Prices are generated tick by
  tick from a random walk; there is no options data, no GEX, no calendar of
  real sessions, and no overnight gap structure to exploit.
- split: the pre-registered **training** split, 105 sessions of 390 ticks.
- one price series, `trades` only. No quotes and no book, so nothing in the
  documents above about spread, mid or depth applies.
- fill model: `unfavorable`. A resting limit fills only on a trade strictly
  through it.
- warmup: 0 sessions. Action cap: 64 actions per callback.
- history window: 512 bars per subscription. Past that, `ctx.history(...)[0]`
  raises; keep what you need on `self`.
- wall-clock budget: 120s for the whole walk. A tick subscription over 105
  sessions will not finish inside it; bar intervals will.
- commission is charged per contract on every fill, so turnover is not free."""

ARMS = {
    "history": True,
    "control": False,
}


# --------------------------------------------------------------------------- #
# stage: register
# --------------------------------------------------------------------------- #

def agent_for(arm: str):
    from src.core.loop.llm import LLMAgent

    return LLMAgent(
        model=MODEL, temperature=TEMPERATURE, max_tokens=MAX_TOKENS,
        show_history=ARMS[arm], max_repair_attempts=MAX_REPAIR_ATTEMPTS,
        request_timeout_s=REQUEST_TIMEOUT_S,
        proposal_wall_clock_s=PROPOSAL_WALL_CLOCK_S, run_facts=RUN_FACTS,
    )


def stage_register() -> None:
    from src.core.loop.campaign import CampaignRecord

    OUT.mkdir(parents=True, exist_ok=True)
    for arm in ARMS:
        path = OUT / arm / "campaign.json"
        if path.exists():
            sys.exit(f"{path} already exists. A pre-registration is written "
                     "once; delete the directory deliberately if you mean to "
                     "start over, and say so in the write-up.")
        record = CampaignRecord(
            campaign_id=f"task36-{arm}",
            profile=PROFILE, budget=BUDGET, q=Q,
            agent=f"src.core.loop.llm.LLMAgent[{arm}]",
            dataset="synthetic", split="train",
            agent_config=agent_for(arm).config(),
            stopping_rule=(
                f"fixed budget of {BUDGET}; no early stop on any observed "
                "result, no extension if the last trial looks promising"),
            note=(
                f"Task 36 {arm} arm. The agent sees the API documents, the "
                f"current strategy and "
                + ("its own prior trials with their scores"
                   if ARMS[arm] else "NOTHING about its own past (the control)")
                + ". It never sees a threshold, a p-value, a false-acceptance "
                "rate, an accept/reject verdict or N. Budget of 100 is below "
                "Task 34's 'at least 400': that recommendation is about "
                "detection power on data with something to detect, and this "
                "dataset has no edge by construction. The binding constraint "
                "here is the trajectory test, whose resolution is in "
                "docs/reviews/task36/resolution.json -- 80% power at a trend "
                "of 1.0 SD across the campaign and at a between-arm shift of "
                "0.4 SD."),
            written_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = record.write(path)
        print(f"{arm:8s} pre-registered  budget {BUDGET}  q {Q}  "
              f"record {digest[:16]}")
        print(f"         {path}")


# --------------------------------------------------------------------------- #
# stage: run
# --------------------------------------------------------------------------- #

def _pregenerate(n: int) -> list[tuple[str, dict]]:
    """The control arm's proposals, concurrently. Valid because it sees no
    history: nothing about proposal i can depend on proposal j."""
    def one(_):
        agent = agent_for("control")
        src = agent.propose([])
        return src, agent.drain_log()

    t0 = time.monotonic()
    out: list[tuple[str, dict]] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for i, r in enumerate(pool.map(one, range(n)), start=1):
            out.append(r)
            if i % 10 == 0:
                print(f"  control proposals {i}/{n}  "
                      f"{time.monotonic() - t0:.0f}s", flush=True)
    return out


def stage_run(only: str | None) -> None:
    from src.core.loop.campaign import (CampaignRecord, CampaignResult,
                                        run_campaign, run_trial)

    for arm in ARMS:
        if only and arm != only:
            continue
        adir = OUT / arm
        if (adir / "result.json").exists():
            print(f"{arm}: already run, skipping")
            continue
        record, digest = CampaignRecord.read(adir / "campaign.json")
        print(f"\n=== {arm} arm: {record.budget} trials, record {digest[:16]} ===",
              flush=True)
        t0 = time.monotonic()

        if ARMS[arm]:
            agent = agent_for(arm)
            result = run_campaign(agent, record, ledger=adir / "ledger.jsonl",
                                  record_path=adir / "campaign.json",
                                  progress=True)
            rows = result.rows
            tampered = result.tampered
        else:
            proposals = _pregenerate(record.budget)
            rows = []
            for i, (source, log) in enumerate(proposals):
                row = run_trial(source, profile=record.profile,
                                ledger=adir / "ledger.jsonl",
                                campaign_id=record.campaign_id, index=i)
                row["agent_call"] = log
                rows.append(row)
                if (i + 1) % 25 == 0:
                    print(f"  {i + 1}/{record.budget} trials, "
                          f"{time.monotonic() - t0:.0f}s", flush=True)
            _, reread = CampaignRecord.read(adir / "campaign.json")
            tampered = reread != digest

        (adir / "result.json").write_text(json.dumps(
            {"rows": rows, "wall_clock_s": time.monotonic() - t0,
             "record_digest": digest, "tampered": tampered}, default=str))
        done = sum(1 for r in rows if r.get("outcome") == "completed")
        print(f"{arm}: {done}/{len(rows)} completed in "
              f"{time.monotonic() - t0:.0f}s")


# --------------------------------------------------------------------------- #
# stage: nulls
# --------------------------------------------------------------------------- #

def _isolated_run(args):
    """One run from a stored source. `rot_seed=None` means the real data.

    Top level so it can cross a process pool. The un-rotated form is how the
    campaign's session returns are recovered -- the ledger row does not carry
    them -- and it doubles as the demonstration behind pre-registered decision
    (d): re-running a trial from its stored source reproduces its score.
    """
    path, rot_seed = args
    sys.path.insert(0, str(REPO))
    from src.core.data.synthetic import SyntheticDataset, SyntheticSpec
    from src.core.engine import Engine
    from src.core.engine.remote import RemoteStrategy
    from src.core.runlog import RunTicket
    from src.core.scoring import metrics as metricsmod
    from src.core.scoring.randomization.rotation import RotatedDataset
    from src.core.scoring.scorer import score
    from src.core.types import Split

    ds = SyntheticDataset(SyntheticSpec(
        n_sessions=N_SESSIONS, ticks_per_session=TICKS, vol_per_tick=VOL,
        seed=DATASET_SEED, signal_strength=0.0))
    if rot_seed is not None:
        ds = RotatedDataset(ds, seed=rot_seed)
    ticket = RunTicket.for_null_calibration("task 36 per-candidate null")
    try:
        with RemoteStrategy(pathlib.Path(path), "Strategy") as strategy:
            engine = Engine(ds, strategy, ticket=ticket, split=Split.TRAIN,
                            warmup_sessions=0, fill_model="unfavorable", seed=1)
            engine.run()
        blotter = engine.reconstructed()
        period = engine.session_calendar()
        s = score(blotter, period=period)
        return {"score": None if s.rejected else s.primary,
                "returns": metricsmod.session_returns(blotter, period)}
    except Exception:
        # A run that crashes contributes nothing rather than a placeholder. A
        # number nobody measured has no business in a null distribution.
        return {"score": None, "returns": []}


def stage_nulls(only: str | None) -> None:
    from src.core.scoring.randomization.hybrid import choose_null
    from src.core.scoring.randomization.profile_null import ProfileNull

    table = ProfileNull.read()
    WORK.mkdir(parents=True, exist_ok=True)

    for arm in ARMS:
        if only and arm != only:
            continue
        adir = OUT / arm
        if (adir / "nulls.json").exists():
            print(f"{arm}: nulls already computed, skipping")
            continue
        rows = json.loads((adir / "result.json").read_text())["rows"]

        # Pass 1: re-run each scored candidate on the real data, from the source
        # the ledger stored. Two things come out -- the session returns the
        # routing needs, which the row does not carry, and a reproduction check
        # on every trial, which is decision (d) demonstrated rather than
        # claimed.
        base_jobs, base_slots = [], []
        for i, row in enumerate(rows):
            score = row.get("score") or {}
            src = row.get("strategy_source")
            if (score.get("primary") is None or score.get("rejected")
                    or not src or not pathlib.Path(src).exists()):
                continue
            base_slots.append(i)
            base_jobs.append((src, None))
        print(f"\n=== {arm}: re-running {len(base_jobs)} scored candidates "
              "to recover returns and check reproduction ===", flush=True)
        base: dict[int, dict] = {}
        with ProcessPoolExecutor(max_workers=WORKERS) as pool:
            for i, r in zip(base_slots, pool.map(_isolated_run, base_jobs,
                                                 chunksize=2)):
                base[i] = r
        mismatches = [
            {"index": i, "ledger": rows[i]["score"]["primary"], "rerun": v["score"]}
            for i, v in base.items()
            if v["score"] is None or v["score"] != rows[i]["score"]["primary"]
        ]
        print(f"    reproduced {len(base) - len(mismatches)}/{len(base)} "
              f"exactly; {len(mismatches)} mismatches")

        routes: list[dict | None] = []
        samples: list[list[float]] = []
        jobs, slots = [], []
        for i, row in enumerate(rows):
            returns = (base.get(i) or {}).get("returns")
            score = row.get("score") or {}
            if not returns or score.get("primary") is None or score.get("rejected"):
                routes.append(None)
                samples.append([])
                continue
            choice = choose_null(returns, table)
            routes.append(choice.as_dict())
            if choice.route == "bucket":
                samples.append(list(table.sample_for(returns)))
            else:
                samples.append([])
                src = row.get("strategy_source")
                if not src or not pathlib.Path(src).exists():
                    continue
                slots.append(i)
                jobs.extend((src, 70_000 + i * 1_000 + k)
                            for k in range(FALLBACK_R))

        share = sum(1 for c in routes if c and c["route"] == "rotation")
        scored = sum(1 for c in routes if c)
        print(f"\n=== {arm}: {scored} scored candidates, "
              f"{share} routed to rotation ({share / max(scored, 1):.1%}) ===")
        print(f"    {len(jobs):,} rotated runs at R={FALLBACK_R}", flush=True)

        if jobs:
            t0 = time.monotonic()
            done = []
            with ProcessPoolExecutor(max_workers=WORKERS) as pool:
                for n, val in enumerate(pool.map(_isolated_run, jobs,
                                                 chunksize=4), start=1):
                    done.append(val)
                    if n % 500 == 0:
                        rate = n / (time.monotonic() - t0)
                        print(f"    {n:,}/{len(jobs):,}  {rate:.1f}/s  eta "
                              f"{(len(jobs) - n) / rate / 60:.0f} min", flush=True)
            for k, i in enumerate(slots):
                chunk = done[k * FALLBACK_R:(k + 1) * FALLBACK_R]
                samples[i] = [v["score"] for v in chunk if v["score"] is not None]

        (adir / "nulls.json").write_text(json.dumps(
            {"routes": routes, "samples": samples, "fallback_r": FALLBACK_R,
             "reproduction": {"checked": len(base),
                              "exact": len(base) - len(mismatches),
                              "mismatches": mismatches[:20]}}))
        print(f"    wrote {adir / 'nulls.json'}")


# --------------------------------------------------------------------------- #
# stage: report
# --------------------------------------------------------------------------- #

def _trajectory(rows: list[dict], b: int = B_PERM) -> dict:
    """The pre-registered trajectory statistics. See docs/reviews/task36/."""
    import numpy as np
    sys.path.insert(0, str(REPO / "docs" / "reviews" / "task36"))
    from resolution import perm_p_shift, perm_p_trend, spearman   # noqa: E402

    rng = np.random.default_rng(36)
    idx = [i for i, r in enumerate(rows)
           if (r.get("score") or {}).get("primary") is not None
           and not (r.get("score") or {}).get("rejected")]
    scores = np.array([rows[i]["score"]["primary"] for i in idx], dtype=float)

    failed = np.array([1.0 if r.get("outcome") != "completed" else 0.0
                       for r in rows])
    half = len(rows) // 2
    running = np.maximum.accumulate(scores) if len(scores) else np.array([])

    out = {
        "n_scored": len(scores),
        "scores_in_order": [round(float(s), 6) for s in scores],
        "scored_trial_indices": idx,
        "running_max": [round(float(v), 6) for v in running],
        "mean": float(scores.mean()) if len(scores) else None,
        "sd": float(scores.std(ddof=1)) if len(scores) > 1 else None,
        "first_half_mean": float(scores[:len(scores) // 2].mean()) if len(scores) > 3 else None,
        "second_half_mean": float(scores[len(scores) // 2:].mean()) if len(scores) > 3 else None,
        "t1_spearman_rho": spearman(np.arange(len(scores), dtype=float), scores)
                           if len(scores) > 3 else None,
        "t1_permutation_p": perm_p_trend(scores, b=b, rng=rng)
                            if len(scores) > 3 else None,
        "t2_failure_rate_first_half": float(failed[:half].mean()),
        "t2_failure_rate_second_half": float(failed[half:].mean()),
    }
    return out


def stage_report() -> None:
    import numpy as np
    from src.core.loop.campaign import CampaignRecord, CampaignResult, summarise
    sys.path.insert(0, str(REPO / "docs" / "reviews" / "task36"))
    from resolution import perm_p_shift   # noqa: E402

    report: dict = {"arms": {}, "written_at":
                    dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    per_arm_scores = {}

    for arm in ARMS:
        adir = OUT / arm
        record, digest = CampaignRecord.read(adir / "campaign.json")
        blob = json.loads((adir / "result.json").read_text())
        nulls = json.loads((adir / "nulls.json").read_text())
        result = CampaignResult(record=record, record_digest=blob["record_digest"],
                                rows=blob["rows"],
                                wall_clock_s=blob["wall_clock_s"],
                                tampered=blob["tampered"])
        summary = summarise(result, dataset="synthetic",
                            null_samples=nulls["samples"],
                            null_routes=nulls["routes"])
        traj = _trajectory(blob["rows"])
        calls = [r.get("agent_call") or {} for r in blob["rows"]]
        summary["agent_cost"] = {
            "prompt_tokens": sum(c.get("prompt_tokens", 0) for c in calls),
            "completion_tokens": sum(c.get("completion_tokens", 0) for c in calls),
            "api_latency_s": round(sum(c.get("latency_s", 0.0) for c in calls), 1),
            "n_calls": sum(c.get("n_calls", 0) for c in calls),
            "repair_attempts": sum(c.get("repair_attempts", 0) for c in calls),
            "blocked_proposals": sum(1 for c in calls if c.get("blocked")),
        }
        summary["trajectory"] = traj
        summary["record_digest_now"] = record.digest()
        summary["record_digest_at_write"] = blob["record_digest"]
        report["arms"][arm] = summary
        per_arm_scores[arm] = np.array(traj["scores_in_order"], dtype=float)

    a, c = per_arm_scores["history"], per_arm_scores["control"]
    if len(a) > 3 and len(c) > 3:
        rng = np.random.default_rng(136)
        report["between_arms"] = {
            "t3_rank_sum_p_history_above_control": perm_p_shift(a, c, b=B_PERM, rng=rng),
            "history_mean": float(a.mean()), "control_mean": float(c.mean()),
            "history_best": float(a.max()), "control_best": float(c.max()),
            "pooled_sd": float(np.concatenate([a, c]).std(ddof=1)),
            "observed_shift_sd": float((a.mean() - c.mean())
                                       / np.concatenate([a, c]).std(ddof=1)),
        }
    (OUT / "report.json").write_text(json.dumps(report, indent=2, default=str))
    print(render(report))
    print(f"\nwrote {OUT / 'report.json'}")


def render(report: dict) -> str:
    L = []
    for arm, s in report["arms"].items():
        t, g, cost = s["trajectory"], s["gate"], s["agent_cost"]
        L.append(f"=== {arm} ===")
        L.append(f"  record        {s['record_digest_at_write'][:16]}"
                 f"   now {s['record_digest_now'][:16]}"
                 + ("   TAMPERED" if s["record_tampered"] else "   unchanged"))
        L.append(f"  N (trials)    {s['n_trials']}   harness failures "
                 f"{s['harness_failures']}   unaccounted {s['unaccounted']}")
        L.append(f"  outcomes      {s['outcomes']}")
        L.append(f"  scored        {t['n_scored']}   mean "
                 f"{t['mean']:+.4f}" if t["mean"] is not None else "  scored 0")
        if "uncorrected" in s:
            u = s["uncorrected"]
            L.append(f"  best          {u['best_score']:+.4f}  nominal far "
                     f"{u['best_far']:.4g}")
        if g.get("is_the_gate"):
            L.append(f"  GATE max-of-N p {g.get('p'):.4g}"
                     f"  (floor {g.get('p_floor'):.4g}"
                     f", at floor: {g.get('at_floor')})"
                     f"   ACCEPTED: {g.get('reject')}")
            L.append(f"       observed best {g.get('observed_best'):+.4f}"
                     f"   null max at q=0.05 "
                     f"{g['null_max_quantiles']['0.05']:+.4f}"
                     f"   nulls used {g.get('n_trials')}")
        else:
            L.append("  GATE          not computed")
        if "null_routing" in s:
            r = s["null_routing"]
            L.append(f"  rotation share {r['rotation_share']:.1%} "
                     f"({r['rotation']} rotation, {r['bucket']} bucket, "
                     f"{r['unrouted']} unrouted)")
        L.append(f"  T1 trend      rho {t['t1_spearman_rho']}  "
                 f"permutation p {t['t1_permutation_p']}")
        L.append(f"  T2 failures   first half {t['t2_failure_rate_first_half']:.2f}"
                 f"   second half {t['t2_failure_rate_second_half']:.2f}")
        L.append(f"  cost          {cost['n_calls']} calls, "
                 f"{cost['repair_attempts']} repairs, "
                 f"{cost['blocked_proposals']} blocked, "
                 f"{cost['prompt_tokens']:,} in / "
                 f"{cost['completion_tokens']:,} out tokens, "
                 f"{cost['api_latency_s'] / 60:.0f} min of API latency")
        L.append("")
    if "between_arms" in report:
        b = report["between_arms"]
        L.append("=== between arms ===")
        L.append(f"  history mean {b['history_mean']:+.4f}   control mean "
                 f"{b['control_mean']:+.4f}   shift "
                 f"{b['observed_shift_sd']:+.3f} SD")
        L.append(f"  T3 rank-sum p (history above control) "
                 f"{b['t3_rank_sum_p_history_above_control']:.4f}")
        L.append("  resolution: 80% power at 0.4 SD "
                 "(docs/reviews/task36/resolution.json)")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", required=True,
                    choices=("register", "run", "nulls", "report"))
    ap.add_argument("--arm", default=None, choices=tuple(ARMS))
    a = ap.parse_args()
    {"register": stage_register,
     "run": lambda: stage_run(a.arm),
     "nulls": lambda: stage_nulls(a.arm),
     "report": stage_report}[a.stage]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
