#!/usr/bin/env python3
"""Task 37: the positive control. Same loop, same agent, signal in the data.

    python scripts/positive_control.py --stage register   # pre-register, hashed
    python scripts/positive_control.py --stage run
    python scripts/positive_control.py --stage nulls
    python scripts/positive_control.py --stage report

Task 36 showed the loop searches and the gate rejects noise. It did not show the
pair finds a real edge, and it could not say whether the history arm's
improvement was research or overfitting speed. This is the experiment that
answers both.

**Only the data changes.** Same model, same temperature, same budget, same
documents, same bounds, and the same prompt template -- asserted by digest
against Task 36's in the campaign record. **The agent is never told a signal
exists.** If the prompt hinted at one, this would measure whether a model can
implement a described rule, which is a different and much easier question.

`scripts/llm_campaign.py` is deliberately left alone. It is the record of what
Task 36 ran, and editing it to serve a second experiment would make that record
a thing that has to be read through a diff.

**The mechanism, and why it is reachable.** `signal_strength` plants momentum
over consecutive 10-tick blocks: the drift of block *i* is
`signal_strength * sign(return of block i-1)`. Ticks are a median 30s apart, so
a block is exactly one 5m bar -- the interval every worked example in
`strategy-api.md` uses. `docs/reviews/task37/mechanism.py` measures the ceiling.

**Stated before the run: the gate is expected to be conservative here, and the
reason is measured rather than guessed.** The fallback null circularly rotates
each session's price increments, and a circular rotation preserves cyclic order,
so a relationship between one block and the next survives everywhere except the
wrap seam. Measured on the oracle: at strength 0.30 the rotated null has mean
+4.96 against an observed +9.82, and at 0.50 it has mean +14.96 with a **maximum
of +54.46 above the observed +38.25**. The null carries between a third and a
half of the planted signal. The gate is not changed in response -- that is a
constraint of this task -- and a non-acceptance therefore does not mean the loop
failed to find the edge. It means the null it is compared against has the edge
too.

Training split only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
import time
from concurrent.futures import ProcessPoolExecutor

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# **Rebound by `--model`, and `task37` is the default rather than the only
# value.** Task 37 is a specific experiment -- the Task 36 proposer, on planted
# data -- and its record is written. Running the same APPARATUS on a different
# proposer is a different experiment and must not land in Task 37's directory
# or borrow its campaign ids, so the tag, the output root and the scratch root
# move together with the model. Defaults reproduce Task 37 exactly.
RUN_TAG = "task37"
OUT = REPO / "runs" / "campaigns" / "task37"
WORK = REPO / "runs" / "tmp-task37"

# ---- the pre-registration ------------------------------------------------- #
BUDGET = 100
Q = 0.05
# The proposer Task 36 ran, which is what Task 37 exists to match. `MODEL` is
# what this invocation will actually use; the two differ only under `--model`.
TASK36_MODEL = "claude-haiku-4-5"
MODEL = TASK36_MODEL
TEMPERATURE = 0.7
MAX_TOKENS = 16_384
MAX_REPAIR_ATTEMPTS = 2
REQUEST_TIMEOUT_S = 240.0
PROPOSAL_WALL_CLOCK_S = 900.0
FALLBACK_R = 150
N_SESSIONS, TICKS, VOL = 175, 390, 0.15
DATASET_SEED = 1
B_PERM = 10_000
WORKERS = 9

# Task 36's template digest, and the one the same template hashes to after Task
# 46 added the `{hypothesis}` slot. Equality against one of them is asserted at
# registration: the whole comparison rests on the agent having been asked the
# same question.
#
# **Why two constants and not a bumped one.** The slot is additive and it sits
# alone on a line, so a campaign with no hypothesis renders the SYSTEM and USER
# messages byte for byte as Task 36 did -- the file moved, what the model
# received did not, and `test_hypothesis.py` asserts exactly that against the
# template as it stood at Task 45. Overwriting the old constant would have
# thrown away the fact that the two are comparable; keeping both, and requiring
# `hypothesis is None`, is the version of this guard that still means something.
#
# A THIRD digest must never be added without re-establishing the same property.
# If someone edits the body of this template, the comparison to Task 36 is over
# and the honest move is to say so and stop, not to append a constant.
TASK36_TEMPLATE_DIGEST = (
    "97e45328af3f9f73a07281de3418127d6fc86e37423c260640413d86f1b1f165")
TASK46_TEMPLATE_DIGEST = (
    "35d79b52fc7b0e6dbc5e49b33fab82c2e0caad54bc17725cd199ee987be42e3e")

# arm -> (profile, signal strength, why this strength)
ARMS = {
    "s030": ("campaign-s030", 0.30,
             "near the bar: the calibration reference scores +0.7708 here "
             "against the Task 36 best-of-100 null maximum of +0.8011, so a "
             "plausible strategy lands on the bar and clearing it takes a "
             "candidate better than the apparatus. Oracle ceiling +9.8190."),
    "s050": ("campaign-s050", 0.50,
             "clearly above the bar: the reference scores +2.8810, 3.6x the "
             "+0.8011 bar. Oracle ceiling +38.2458."),
    "s000": ("campaign", 0.00,
             "no planted signal at all -- the false-accept check, on the same "
             "`synthetic` dataset and `campaign` profile Task 36's arms ran on. "
             "Without it, a proposer that scores higher on EVERYTHING is "
             "indistinguishable from one that searches better, and the planted "
             "arms cannot say which they measured. The haiku equivalent is "
             "Task 36's `history` arm (show_history=True, as here): best "
             "+0.4222, rho +0.2834, gate p 0.6859, not accepted."),
}

RUN_FACTS_TEMPLATE = """- dataset: a **simulated** instrument, not NQ. Prices are generated tick by
  tick; there is no options data, no GEX, no calendar of real sessions.
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


def agent_for(arm: str):
    from src.core.loop.llm import LLMAgent

    return LLMAgent(
        model=MODEL, temperature=TEMPERATURE, max_tokens=MAX_TOKENS,
        show_history=True, max_repair_attempts=MAX_REPAIR_ATTEMPTS,
        request_timeout_s=REQUEST_TIMEOUT_S,
        proposal_wall_clock_s=PROPOSAL_WALL_CLOCK_S,
        run_facts=RUN_FACTS_TEMPLATE,
    )


def stage_register(only: str | None = None) -> None:
    from src.core.loop.campaign import CampaignRecord
    from src.core.loop.llm import template_digest

    digest = template_digest()
    if digest not in (TASK36_TEMPLATE_DIGEST, TASK46_TEMPLATE_DIGEST):
        sys.exit(
            f"the prompt template has changed since Task 36.\n"
            f"  now      {digest}\n  task 36  {TASK36_TEMPLATE_DIGEST}\n"
            f"  task 46  {TASK46_TEMPLATE_DIGEST}\n"
            "The positive control compares against Task 36's campaigns, and a "
            "different prompt makes that comparison meaningless. Restore the "
            "template or abandon the comparison explicitly -- do not proceed."
        )
    OUT.mkdir(parents=True, exist_ok=True)
    wanted = ARMS if only is None else {only: ARMS[only]}
    for arm, (profile, strength, why) in wanted.items():
        path = OUT / arm / "campaign.json"
        if path.exists():
            sys.exit(f"{path} already exists. A pre-registration is written "
                     "once.")
        # **A planted arm may not be registered once another arm has results,
        # and a zero arm may.** The asymmetry is the whole point. Choosing a
        # signal STRENGTH after seeing what another strength scored is picking
        # the experiment to suit the answer. A zero arm has no strength to
        # choose -- the parameter is 0.00 or it is not a control -- and it can
        # only ever weaken the claim the planted arms made, never strengthen
        # it: if noise is accepted too, their acceptance meant less than it
        # looked. A check that can only cost you something is safe to add late.
        if strength > 0:
            ran = [a for a in ARMS if a != arm
                   and (OUT / a / "ledger.jsonl").exists()]
            if ran:
                sys.exit(
                    f"refusing to register {arm} (strength {strength}) now: "
                    f"{', '.join(ran)} already has trials on disk.\n"
                    f"Registering a planted strength after seeing another "
                    f"strength's results is choosing the experiment to suit "
                    f"the answer. Register the planted arms together, before "
                    f"any of them runs.")
        agent = agent_for(arm)
        if agent.hypothesis is not None:
            sys.exit(
                "this control runs without a hypothesis, because Task 36's "
                "campaigns had none and a hypothesis changes what the agent was "
                "asked. Comparing across that is not a positive control.")
        cfg = agent.config()
        # True for either digest, and it is a claim about the MESSAGES rather
        # than about the file -- see the constants above.
        cfg["prompt_template_digest_matches_task36"] = True
        cfg["prompt_template_digest_is_task46_slot_only"] = (
            digest == TASK46_TEMPLATE_DIGEST)
        cfg["signal_strength"] = strength
        cfg["planted_mechanism"] = (
            "momentum over consecutive signal_interval_ticks=10 blocks: the "
            "drift of block i is signal_strength * sign(return of block i-1), "
            "spread over the block. Ticks are a median 30s apart, so a block "
            "is one 5m bar.")
        cfg["agent_told_a_signal_exists"] = False
        # **Whether this is Task 37 or only shares its apparatus.** Task 37's
        # claim rests on the proposer being Task 36's, so a different one
        # cannot be compared to Task 36's no-signal arms to separate search
        # from self-flattery -- only to the OTHER arms of its own run and to
        # Task 37's numbers at the same planted strength.
        cfg["is_task36_proposer"] = (MODEL == TASK36_MODEL)
        cfg["task36_baseline_comparable"] = (MODEL == TASK36_MODEL)
        record = CampaignRecord(
            campaign_id=f"{RUN_TAG}-{arm}",
            profile=profile, budget=BUDGET, q=Q,
            agent=f"src.core.loop.llm.LLMAgent[{arm}]",
            dataset=f"synthetic-{arm}", split="train",
            agent_config=cfg,
            stopping_rule=(
                f"fixed budget of {BUDGET}; no early stop on any observed "
                "result, no extension if the last trial looks promising"),
            note=(
                f"Task 37 positive control, planted strength {strength}. "
                f"Chosen {why} Success takes BOTH conditions: the gate accepts "
                "at q, AND the accepted candidate exploits the planted "
                "mechanism rather than happening to look good. Stated before "
                "running: the rotation null preserves cyclic order and "
                "therefore retains between a third and a half of this signal "
                "(docs/reviews/task37/rotation-check.json), so the gate is "
                "expected to be conservative and a non-acceptance does not by "
                "itself mean the loop failed to find the edge."),
            written_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        d = record.write(path)
        print(f"{arm}  strength {strength}  profile {profile}  budget {BUDGET}"
              f"  record {d[:16]}")
    print(f"prompt template digest {digest[:16]} == Task 36's. Asserted.")


def stage_run(only: str | None) -> None:
    from src.core.loop.campaign import CampaignRecord, run_campaign

    # Before anything is billed: is the endpoint serving the model the records
    # name? Task 37's first attempt at the 0.30 arm was discarded because it was
    # not -- `claude-haiku-4-5` began returning 503 and the gateway answered 200
    # with `gemini-3.1-pro-preview`. Nothing recorded the model that served each
    # call, so how much of that run was affected could not be established. The
    # per-call record and this probe are both new because of it.
    probe = agent_for(next(iter(ARMS))).preflight()
    print(f"preflight: asked for {probe['requested']!r}, endpoint served "
          f"{probe['served']!r}", flush=True)

    for arm in ARMS:
        if only and arm != only:
            continue
        adir = OUT / arm
        if (adir / "result.json").exists():
            print(f"{arm}: already run, skipping")
            continue
        record, digest = CampaignRecord.read(adir / "campaign.json")
        print(f"\n=== {arm}: {record.budget} trials on {record.profile}, "
              f"record {digest[:16]} ===", flush=True)
        t0 = time.monotonic()
        result = run_campaign(agent_for(arm), record,
                              ledger=adir / "ledger.jsonl",
                              record_path=adir / "campaign.json", progress=True)
        (adir / "result.json").write_text(json.dumps(
            {"rows": result.rows, "wall_clock_s": time.monotonic() - t0,
             "record_digest": digest, "tampered": result.tampered}, default=str))
        done = sum(1 for r in result.rows if r.get("outcome") == "completed")
        print(f"{arm}: {done}/{len(result.rows)} completed in "
              f"{time.monotonic() - t0:.0f}s")


def _isolated_run(args):
    """One run from a stored source, on this arm's data. `rot_seed=None` is the
    real path, and doubles as the reproduction check."""
    path, rot_seed, strength = args
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
        seed=DATASET_SEED, signal_strength=strength))
    if rot_seed is not None:
        ds = RotatedDataset(ds, seed=rot_seed)
    ticket = RunTicket.for_null_calibration("task 37 per-candidate null")
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
        return {"score": None, "returns": []}


def stage_nulls(only: str | None) -> None:
    from src.core.scoring.randomization.hybrid import choose_null
    from src.core.scoring.randomization.profile_null import ProfileNull

    table = ProfileNull.read()
    WORK.mkdir(parents=True, exist_ok=True)
    for arm, (_, strength, _why) in ARMS.items():
        if only and arm != only:
            continue
        adir = OUT / arm
        if (adir / "nulls.json").exists():
            print(f"{arm}: nulls already computed, skipping")
            continue
        rows = json.loads((adir / "result.json").read_text())["rows"]

        base_jobs, base_slots = [], []
        for i, row in enumerate(rows):
            score = row.get("score") or {}
            src = row.get("strategy_source")
            if (score.get("primary") is None or score.get("rejected")
                    or not src or not pathlib.Path(src).exists()):
                continue
            base_slots.append(i)
            base_jobs.append((src, None, strength))
        print(f"\n=== {arm}: re-running {len(base_jobs)} scored candidates ===",
              flush=True)
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
                jobs.extend((src, 70_000 + i * 1_000 + k, strength)
                            for k in range(FALLBACK_R))

        rot = sum(1 for c in routes if c and c["route"] == "rotation")
        scored = sum(1 for c in routes if c)
        print(f"    {scored} scored, {rot} routed to rotation "
              f"({rot / max(scored, 1):.1%}); {len(jobs):,} rotated runs "
              f"at R={FALLBACK_R}", flush=True)
        if jobs:
            t0 = time.monotonic()
            done = []
            with ProcessPoolExecutor(max_workers=WORKERS) as pool:
                for n, val in enumerate(pool.map(_isolated_run, jobs,
                                                 chunksize=4), start=1):
                    done.append(val)
                    if n % 1000 == 0:
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


def stage_report() -> None:
    import numpy as np
    from src.core.loop.campaign import CampaignRecord, CampaignResult, summarise
    sys.path.insert(0, str(REPO / "scripts"))
    sys.path.insert(0, str(REPO / "docs" / "reviews" / "task36"))
    from llm_campaign import _trajectory                       # noqa: E402

    report: dict = {"arms": {}, "task36_reference": {}, "written_at":
                    dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    for arm, (_, strength, why) in ARMS.items():
        adir = OUT / arm
        record, _ = CampaignRecord.read(adir / "campaign.json")
        blob = json.loads((adir / "result.json").read_text())
        nulls = json.loads((adir / "nulls.json").read_text())
        result = CampaignResult(record=record, record_digest=blob["record_digest"],
                                rows=blob["rows"],
                                wall_clock_s=blob["wall_clock_s"],
                                tampered=blob["tampered"])
        summary = summarise(result, dataset="synthetic",
                            null_samples=nulls["samples"],
                            null_routes=nulls["routes"])
        summary["signal_strength"] = strength
        summary["why_this_strength"] = why
        summary["trajectory"] = _trajectory(blob["rows"], b=B_PERM)
        summary["reproduction"] = nulls["reproduction"]
        calls = [r.get("agent_call") or {} for r in blob["rows"]]
        summary["agent_cost"] = {
            "prompt_tokens": sum(c.get("prompt_tokens", 0) for c in calls),
            "completion_tokens": sum(c.get("completion_tokens", 0) for c in calls),
            "api_latency_s": round(sum(c.get("latency_s", 0.0) for c in calls), 1),
            "n_calls": sum(c.get("n_calls", 0) for c in calls),
            "repair_attempts": sum(c.get("repair_attempts", 0) for c in calls),
            "blocked_proposals": sum(1 for c in calls if c.get("blocked")),
        }
        summary["record_digest_now"] = record.digest()
        # Which candidate the gate would be accepting, so attribution has
        # something to inspect. Named, not judged, here.
        scored = [(i, r["score"]["primary"], r.get("strategy_source"))
                  for i, r in enumerate(blob["rows"])
                  if (r.get("score") or {}).get("primary") is not None
                  and not r["score"].get("rejected")]
        best = max(scored, key=lambda x: x[1])
        summary["best_candidate"] = {"index": best[0], "score": best[1],
                                     "source": best[2]}
        report["arms"][arm] = summary

    t36 = REPO / "runs" / "campaigns" / "task36" / "report.json"
    if t36.exists():
        d = json.loads(t36.read_text())
        for arm in ("history", "control"):
            t = d["arms"][arm]["trajectory"]
            report["task36_reference"][arm] = {
                "t1_spearman_rho": t["t1_spearman_rho"],
                "t1_permutation_p": t["t1_permutation_p"],
                "mean": t["mean"], "sd": t["sd"],
                "best": max(t["scores_in_order"]),
                "gate_p": d["arms"][arm]["gate"].get("p"),
                "rotation_share": d["arms"][arm]["null_routing"]["rotation_share"],
            }
    (OUT / "report.json").write_text(json.dumps(report, indent=2, default=str))
    print(render(report))
    print(f"\nwrote {OUT / 'report.json'}")


def render(report: dict) -> str:
    L = []
    for arm, s in report["arms"].items():
        t, g, c = s["trajectory"], s["gate"], s["agent_cost"]
        L.append(f"=== {arm}  (planted strength {s['signal_strength']}) ===")
        L.append(f"  record        {s['record_digest_now'][:16]}"
                 + ("   TAMPERED" if s["record_tampered"] else "   unchanged"))
        L.append(f"  N             {s['n_trials']}   harness failures "
                 f"{s['harness_failures']}   unaccounted {s['unaccounted']}")
        L.append(f"  outcomes      {s['outcomes']}")
        L.append(f"  reproduction  {s['reproduction']['exact']}/"
                 f"{s['reproduction']['checked']} exact")
        if t["mean"] is not None:
            L.append(f"  scored        {t['n_scored']}   mean {t['mean']:+.4f}"
                     f"   sd {t['sd']:.4f}")
        if "uncorrected" in s:
            u = s["uncorrected"]
            L.append(f"  best          {u['best_score']:+.4f}  (trial "
                     f"#{s['best_candidate']['index']})   nominal far "
                     f"{u['best_far']:.4g}")
        if g.get("is_the_gate"):
            L.append(f"  GATE          max-of-N p {g['p']:.4g}   "
                     f"ACCEPTED: {g['reject']}")
            L.append(f"                observed {g['observed_best']:+.4f}  "
                     f"null max q05 {g['null_max_quantiles']['0.05']:+.4f}  "
                     f"null max mean {g['null_max_mean']:+.4f}")
        if "null_routing" in s:
            r = s["null_routing"]
            L.append(f"  rotation      {r['rotation_share']:.1%} "
                     f"({r['rotation']} rotation, {r['bucket']} bucket)")
        L.append(f"  T1 trend      rho {t['t1_spearman_rho']:+.4f}   "
                 f"permutation p {t['t1_permutation_p']:.4f}")
        L.append(f"  halves        {t['first_half_mean']:+.4f} -> "
                 f"{t['second_half_mean']:+.4f}")
        L.append(f"  cost          {c['n_calls']} calls, {c['repair_attempts']} "
                 f"repairs, {c['blocked_proposals']} blocked, "
                 f"{c['api_latency_s'] / 60:.0f} min latency")
        L.append("")
    if report.get("task36_reference"):
        L.append("=== Task 36, for comparison (no signal) ===")
        for arm, d in report["task36_reference"].items():
            L.append(f"  {arm:8s} rho {d['t1_spearman_rho']:+.4f} p "
                     f"{d['t1_permutation_p']:.4f}   mean {d['mean']:+.4f}   "
                     f"best {d['best']:+.4f}   gate p {d['gate_p']:.4g}   "
                     f"rotation {d['rotation_share']:.1%}")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", required=True,
                    choices=("register", "run", "nulls", "report"))
    ap.add_argument("--arm", default=None, choices=tuple(ARMS))
    ap.add_argument("--model", default=MODEL,
                    help="the proposer. Defaults to Task 36's, which is what "
                         "makes a run Task 37. Any other value writes to its "
                         "own campaign directory and is a different experiment.")
    ap.add_argument("--max-tokens", type=int, default=MAX_TOKENS,
                    help="output budget. Task 37's 16,384 is right for "
                         "claude-haiku-4-5 and far too small for a reasoning "
                         "model: deepseek-v4-pro was measured emitting 88,799 "
                         "completion tokens on one call, 76,337 of them "
                         "reasoning. Size it from measurement, not from habit.")
    a = ap.parse_args()

    if a.model != MODEL or a.max_tokens != MAX_TOKENS:
        import re as _re
        g = globals()
        g["MODEL"] = a.model
        g["MAX_TOKENS"] = a.max_tokens
        if a.model != TASK36_MODEL:
            tag = "positive-control-" + _re.sub(r"[^a-z0-9.]+", "-", a.model.lower())
            g["RUN_TAG"] = tag
            g["OUT"] = REPO / "runs" / "campaigns" / tag
            g["WORK"] = REPO / "runs" / f"tmp-{tag}"
            print(f"proposer {a.model} is not Task 36's ({TASK36_MODEL}).\n"
                  f"  This is the same apparatus, not Task 37: writing to "
                  f"{g['OUT'].relative_to(REPO)}\n"
                  f"  max_tokens {a.max_tokens:,}", flush=True)
    {"register": lambda: stage_register(a.arm),
     "run": lambda: stage_run(a.arm),
     "nulls": lambda: stage_nulls(a.arm),
     "report": stage_report}[a.stage]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
