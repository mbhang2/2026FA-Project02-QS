#!/usr/bin/env python3
"""Write the tier-2 campaign summary — the artifact tier 3 reads.

    python scripts/campaign_summary.py runs/campaigns/task36/history
    python scripts/campaign_summary.py runs/campaigns/task36/history --check

**The contract between the tiers is a file, and that is the whole design.** The
loop already reaches the harness this way: it writes
`src/strategies/current.py` and invokes `run.py`, importing nothing from
`src.core`, which is why `Gowtam/L1/loop/evaluate_step.py` could become a second
driver without touching anything. The same shape holds here. Tier 2 writes this
artifact; tier 3 reads it; neither imports the other, so either can be replaced
without the other noticing.

**What tier 3 must be able to see, and why each field is here.** Named rather
than dumped, because an artifact that is "everything we had" is one nobody can
tell is complete:

* the campaign id and the **pre-registered record's digest**, so an auditor can
  check the budget and q were fixed before the first trial rather than after;
* **N as the ledger defines it** — every trial, including the crashed ones.
  Tier 2 owns the correction, so this number is tier 3's to display and
  sanity-check and never to recompute a bar from;
* the gate's verdict, the observed best and the **simulated best-of-N bar**, so
  the accept decision can be re-read rather than taken on trust;
* the best candidate's **source digest and its source**, because leakage review
  is one of tier 3's jobs and it cannot review code it cannot see;
* the per-trial diagnostics the harness already records — drawdown, profit
  concentration, seasonality, turnover, fill-model spread, regime divergence —
  which are the inputs to gates `max_of_n` cannot express;
* the **provenance block**: code fingerprint, artifact digests, environment
  digest, lockfile digest, isolation level. An audit of a run whose code cannot
  be identified is an audit of nothing;
* the ledger rows summarised, **by reference** — digest and index, not content.
  A summary that inlines its evidence is a summary that can disagree with it.

**What is deliberately absent: the holdout.** No split identity beyond the name
of the one the campaign ran on, no holdout data, no path to it. An auditor whose
verdict could depend on data the gate has not already conditioned on is an
auditor spending a budget nobody pre-registered.

This is a `scripts/` job. `src/core/**` is fingerprinted and a campaign summary
is derived from ledger rows rather than an input to them.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# **The summary is DIGESTED, so its field set is versioned rather than edited.**
# `tests/test_tier_contract.py::test_the_summary_rebuilds_to_the_same_digest`
# rebuilds a committed summary and compares digests; appending a diagnostic to
# the published set changes that digest and breaks the audit chain for every
# summary already written. Adding a schema version instead keeps old summaries
# verifying under the field set they were written with, and `build` rebuilds
# under whatever schema the stored summary declares.
SCHEMA = "tier2-campaign-summary/2"

# Diagnostics the summary carries per candidate. Named explicitly: a `dict(**d)`
# would ship whatever the engine happened to record, and tier 3 would silently
# start depending on a field nobody meant to publish.
# Frozen. Every tier2-summary/1 on disk was digested over exactly this set.
V1_DIAGNOSTIC_FIELDS = (
    "max_drawdown",
    "profit_concentration_top5pct",
    "best_session_share",
    "turnover_contracts_per_session",
    "avg_holding_period_ns",
    "sessions_with_exposure",
    "win_rate",
    "trade_count",
    "commission",
    "gross_pnl",
    "fill_model_spread",
    "null_reference_divergence",
    "null_assumption",
    "seasonality_caveat",
    "by_weekday",
    "by_month",
    "by_hour_utc",
    # **`by_hour_utc_entry` was computed and then withheld.** The
    # `seasonality_caveat` string the committee already receives instructs the
    # reader to "read `by_hour_utc` against `by_hour_utc_entry` before calling
    # anything concentrated" -- against a field this allowlist did not carry.
    # spx-arb-01's robustness VETO was a by_hour_utc concentration objection
    # raised without the field that exists to contextualise it.
)

# tier2-summary/2 adds three fields and removes none.
V2_DIAGNOSTIC_FIELDS = V1_DIAGNOSTIC_FIELDS + (
    # **`by_hour_utc_entry` was computed and then withheld.** The
    # `seasonality_caveat` string the committee already receives instructs the
    # reader to "read `by_hour_utc` against `by_hour_utc_entry` before calling
    # anything concentrated" -- against a field this allowlist did not carry.
    # spx-arb-01's robustness VETO was a by_hour_utc concentration objection
    # raised without the field that exists to contextualise it.
    "by_hour_utc_entry",
    # The drop-k fragility probe. Published because five of the twelve
    # unresolved deferrals are the robustness seat objecting to concentration
    # with no bar to discharge the objection against.
    "sessions_to_zero",
    "primary_after_drop",
)

SCHEMA_DIAGNOSTICS = {
    "tier2-campaign-summary/1": V1_DIAGNOSTIC_FIELDS,
    "tier2-campaign-summary/2": V2_DIAGNOSTIC_FIELDS,
}

# What a fresh build publishes.
DIAGNOSTIC_FIELDS = SCHEMA_DIAGNOSTICS[SCHEMA]

PROVENANCE_FIELDS = (
    "code_fingerprint",
    "artifact_fingerprint",
    "environment_fingerprint",
    "lockfile_digest",
    "scoring_fingerprint",
    "git_sha",
    "python_version",
)


def digest_of(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _null_moments(arm_dir: Path) -> dict:
    """Mean and sd of the pooled per-candidate null scores.

    Here so tier 3 can run a **deflation cross-check without recomputing the
    correction**. Deflated Sharpe needs E[max of N draws from the null], which
    needs the null's location and scale; the harness simulates that maximum
    directly and daksh's tree has the closed form. Publishing two numbers lets
    the two routes be compared on a real campaign instead of on synthetic input.

    Pooled across candidates on purpose. Each candidate's null is its own under
    the conditional framing, and a cross-check of the *campaign's* bar wants the
    distribution the campaign maximum was taken over.

    **What is not here: the best candidate's return series.** PSR needs it and a
    ledger row does not carry one -- `diagnostics` has aggregates, not
    `session_returns` -- so a full PSR would mean re-running the candidate. Task
    36 recorded that gap; this is where it bites.
    """
    import statistics as st

    path = arm_dir / "nulls.json"
    if not path.exists():
        return {"available": False,
                "why": "no nulls.json; the campaign has no per-candidate nulls"}
    samples = [v for s in json.loads(path.read_text())["samples"] for v in s]
    if len(samples) < 2:
        return {"available": False, "why": "fewer than two null draws"}
    return {
        "available": True,
        "n_draws": len(samples),
        "mean": st.fmean(samples),
        "sd": st.stdev(samples),
        "min": min(samples),
        "max": max(samples),
        "note": ("pooled across candidates. For a deflation cross-check only -- "
                 "tier 2 owns the correction and tier 3 does not recompute a "
                 "bar from these."),
    }


def build(arm_dir: Path, *, schema: str | None = None) -> dict:
    """Rebuild a summary. With no `schema`, use the one the stored summary
    declares so a rebuild-and-compare verifies rather than fails; absent a
    stored summary, write the current one."""
    from src.core.loop.campaign import TRIAL_OUTCOMES, CampaignRecord

    if schema is None:
        stored_path = arm_dir / "tier2-summary.json"
        schema = SCHEMA
        if stored_path.exists():
            try:
                schema = json.loads(stored_path.read_text()).get("schema") or SCHEMA
            except (OSError, json.JSONDecodeError):
                schema = SCHEMA
    if schema not in SCHEMA_DIAGNOSTICS:
        raise ValueError(
            f"unknown summary schema {schema!r}; known: "
            f"{sorted(SCHEMA_DIAGNOSTICS)}")
    diagnostic_fields = SCHEMA_DIAGNOSTICS[schema]

    record, record_digest = CampaignRecord.read(arm_dir / "campaign.json")
    blob = json.loads((arm_dir / "result.json").read_text())
    rows = blob["rows"]
    # Two shapes produce a campaign: `scripts/llm_campaign.py`, which writes a
    # multi-arm `report.json` beside the arms, and a single campaign whose
    # `summarise()` output lands in its own directory as `summary.json`. The
    # second form had no reader until the Task 44 end-to-end run went through
    # it and came out with an empty outcome map and no gate -- which looked
    # like the campaign had no gate rather than like this function had no
    # source. Both are read now, the sibling report first.
    report_path = arm_dir.parent / "report.json"
    report = json.loads(report_path.read_text()) if report_path.exists() else {}
    arm = report.get("arms", {}).get(arm_dir.name, {})
    if not arm and (arm_dir / "summary.json").exists():
        arm = json.loads((arm_dir / "summary.json").read_text())

    trials = [r for r in rows if r.get("outcome") in TRIAL_OUTCOMES]
    # Off-question trials under a registered `enforce` are not candidates for
    # the best -- the same set `summarise` keeps off the gate's observed side,
    # so the candidate the committee reads is the one the gate tested.
    from src.core.loop.fidelity import excluded_trials

    off = excluded_trials(arm_dir, record.agent_config)
    scored = [(i, r) for i, r in enumerate(rows)
              if (r.get("score") or {}).get("primary") is not None
              and not r["score"].get("rejected")
              and r.get("campaign_index", i) not in off]
    best_i, best_row = (max(scored, key=lambda p: p[1]["score"]["primary"])
                        if scored else (None, None))

    gate = dict(arm.get("gate") or {})
    accepted = bool(gate.get("is_the_gate")) and bool(gate.get("reject"))

    # Provenance is taken from a completed trial and asserted uniform across
    # them: a campaign whose rows disagree about the code that produced them is
    # not one summary's worth of anything.
    reference = next((r for r in rows if r.get("outcome") == "completed"), rows[0])
    provenance = {k: reference.get(k) for k in PROVENANCE_FIELDS}
    provenance["isolation"] = reference.get("isolation")
    provenance["artifacts"] = reference.get("artifacts")
    drift = sorted({r.get("code_fingerprint") for r in trials
                    if r.get("code_fingerprint")})
    provenance["code_fingerprint_unique_across_trials"] = len(drift) == 1

    summary = {
        "schema": schema,
        "campaign": {
            "campaign_id": record.campaign_id,
            "arm": arm_dir.name,
            "profile": record.profile,
            "dataset": record.dataset,
            "split": record.split,
            "budget": record.budget,
            "q": record.q,
            "stopping_rule": record.stopping_rule,
            "agent": record.agent,
            "agent_config": record.agent_config,
            "record_digest": record_digest,
            "record_digest_now": record.digest(),
            "record_tampered": bool(blob.get("tampered")),
            "written_at": record.written_at,
        },
        "n": {
            "trials": len(trials),
            "note": ("every trial including crashes; the ledger's definition. "
                     "Tier 2 owns the correction -- this number is for display "
                     "and for sanity-checking, never for recomputing a bar."),
            "by_outcome": arm.get("outcomes") or {},
            "harness_failures": arm.get("harness_failures", 0),
            "unaccounted": arm.get("unaccounted", 0),
        },
        "gate": {
            "is_the_gate": bool(gate.get("is_the_gate")),
            "verdict": ("ACCEPT" if accepted else "REJECT"),
            "accepted_any": accepted,
            "p": gate.get("p"),
            "p_floor": gate.get("p_floor"),
            "at_floor": gate.get("at_floor"),
            "q": gate.get("q", record.q),
            "observed_best": gate.get("observed_best"),
            "null_max_at_q": (gate.get("null_max_quantiles") or {}).get(str(record.q)),
            "null_max_quantiles": gate.get("null_max_quantiles"),
            "null_max_mean": gate.get("null_max_mean"),
            "b": gate.get("b"),
            "n_nulls": gate.get("n_trials"),
            "note": gate.get("note"),
        },
        "null_moments": _null_moments(arm_dir),
        "routing": arm.get("null_routing"),
        "deflation_view": arm.get("deflation"),
        "diagnostics_only": arm.get("diagnostics_only"),
        "best_candidate": None,
        "provenance": provenance,
        "ledger": {
            "path": str((arm_dir / "ledger.jsonl").relative_to(REPO_ROOT)),
            "note": ("rows by reference, not content: a summary that inlines "
                     "its evidence is a summary that can disagree with it"),
            "rows": [
                {"campaign_index": r.get("campaign_index", i),
                 "strategy_digest": r.get("strategy_digest"),
                 "outcome": r.get("outcome"),
                 "score": (r.get("score") or {}).get("primary"),
                 "fill_log_digest": r.get("fill_log_digest")}
                for i, r in enumerate(rows)
            ],
        },
        "holdout": {
            "seen": False,
            "note": ("no holdout data, no path to it, and no split identity "
                     "beyond the name of the one this campaign ran on. An "
                     "auditor whose verdict could depend on data the gate has "
                     "not conditioned on is spending a budget nobody "
                     "pre-registered."),
        },
        "written_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }

    if best_row is not None:
        source_path = best_row.get("strategy_source")
        source = ""
        if source_path and Path(source_path).exists():
            source = Path(source_path).read_text()
        summary["best_candidate"] = {
            "accepted": accepted,
            "campaign_index": best_row.get("campaign_index", best_i),
            "strategy_digest": best_row.get("strategy_digest"),
            "score": best_row.get("score"),
            "source": source,
            "source_sha256": hashlib.sha256(source.encode()).hexdigest() if source else None,
            "diagnostics": {k: (best_row.get("diagnostics") or {}).get(k)
                            for k in diagnostic_fields},
            "note": ("the best-scoring candidate. `accepted` is the gate's "
                     "verdict on it, and is false whenever the campaign "
                     "accepted nothing -- which is most campaigns."),
        }

    summary["digest"] = digest_of({k: v for k, v in summary.items()
                                   if k != "written_at"})
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("arm_dir", type=Path,
                    help="a campaign arm directory holding campaign.json and "
                         "result.json")
    ap.add_argument("--check", action="store_true",
                    help="rebuild and compare against the committed summary")
    a = ap.parse_args(argv)

    arm_dir = a.arm_dir.resolve()
    summary = build(arm_dir)
    out = arm_dir / "tier2-summary.json"
    if a.check:
        if not out.exists():
            print(f"{out} does not exist", file=sys.stderr)
            return 1
        stored = json.loads(out.read_text())
        same = stored.get("digest") == summary["digest"]
        print(f"{out}: {'current' if same else 'STALE'}  "
              f"stored {str(stored.get('digest'))[:16]}  "
              f"rebuilt {summary['digest'][:16]}")
        return 0 if same else 1

    out.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n")
    g = summary["gate"]
    print(f"campaign   {summary['campaign']['campaign_id']} "
          f"({summary['campaign']['arm']})")
    print(f"N          {summary['n']['trials']}   {summary['n']['by_outcome']}")
    print(f"gate       {g['verdict']}   p {g['p']}   observed "
          f"{g['observed_best']}   bar {g['null_max_at_q']}")
    print(f"digest     {summary['digest'][:16]}")
    print(f"wrote      {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
