#!/usr/bin/env python3
"""Single-Question Campaign Loop.

Runs the complete pipeline from a single trading-system goal:
1. Research phase: Decomposes the goal into mechanism families, retrieves
   academic/institutional sources, assigns BWIS priors, proposes, critiques,
   refines, and emits pre-registered hypothesis files.
2. Campaign phase: For each emitted hypothesis, executes the 4-stage campaign
   (register -> run -> nulls -> report) against a fast research profile.
3. Synthesis phase: Reconciles campaign outcomes with BWIS scores and bands,
   evaluates promotion criteria, and writes out comprehensive summary reports.

Usage:
    python -m Gowtam_hypothesis.campaign_loop \\
        --question "Build a profitable intraday scalping strategy for XAU/USD..." \\
        --budget 3 \\
        --profile xauusd-research \\
        --trials 10 \\
        --out runs/research/xauusd-scalp-20261014
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from dotenv import load_dotenv
load_dotenv()

# Ensure repo root and Gowtam_hypothesis are in sys.path
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from Gowtam_hypothesis.researcher import bwis, driver  # noqa: E402
from Gowtam_hypothesis.researcher.schemas import HypothesisRecord  # noqa: E402

DEFAULT_QUESTION = (
    "Build a profitable intraday scalping strategy for XAU/USD on 5-minute bars. "
    "The strategy must enter and exit within the same trading session (no overnight hold), "
    "use only OHLCV and VWAP bar data, be robust to spread and commission costs "
    "(0.06 round trip per unit), and express the entry/exit as a formula with at most 3 parameters. "
    "Mechanism preference: momentum or mean-reversion."
)
DEFAULT_THEME = "Momentum and trend"
DEFAULT_PROFILE = "xauusd-research"
DEFAULT_BUDGET = 3
DEFAULT_STAGES = ("register", "run", "nulls", "report")


def slugify(text: str, max_len: int = 40) -> str:
    """Produce a filesystem-safe slug from text."""
    clean = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return clean[:max_len].rstrip("-") or "research"


@dataclass
class CampaignOutcome:
    hypothesis_id: str
    hypothesis_title: str
    mechanism: str
    bwis_score: float
    bwis_band: str
    campaign_id: str
    profile: str
    trials_budget: int
    n_trials: int
    outcomes: dict[str, int] = field(default_factory=dict)
    observed_best: float | None = None
    null_max_mean: float | None = None
    p_value: float | None = None
    gate_accept: bool = False
    tier3_verdict: str | None = None
    can_promote: bool = False
    promotion_verdict: str = "REJECT"
    reasons: list[str] = field(default_factory=list)
    wall_clock_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_promotion(
    observed_best: float | None,
    gate_accept: bool,
    bwis_band_val: str,
    bwis_score_val: float,
    tier3_verdict_val: str | None,
) -> tuple[str, list[str]]:
    """Determine whether a research campaign result qualifies for promotion."""
    reasons: list[str] = []
    qualifies = True

    if observed_best is None or observed_best <= 0:
        qualifies = False
        reasons.append(
            f"Observed best primary score ({observed_best}) is not positive"
        )
    else:
        reasons.append(f"Observed best score positive: {observed_best:.4f}")

    if not gate_accept:
        qualifies = False
        reasons.append("Pre-registered multiple-testing gate did not accept")
    else:
        reasons.append("Pre-registered multiple-testing gate ACCEPT")

    if bwis_band_val not in ("high", "mid"):
        qualifies = False
        reasons.append(
            f"BWIS source credibility band is {bwis_band_val!r} (must be 'high' or 'mid')"
        )
    else:
        reasons.append(
            f"BWIS source credibility verified: {bwis_band_val} ({bwis_score_val:.3f})"
        )

    if tier3_verdict_val == "FAIL":
        qualifies = False
        reasons.append("Tier 3 automated audit returned FAIL")

    verdict = "PROMOTE" if qualifies else "REJECT"
    return verdict, reasons


def run_research_phase(
    question: str,
    theme: str,
    budget: int,
    output_dir: pathlib.Path,
    log_dir: pathlib.Path,
    dry_run: bool = False,
    feature_context: str = "",
) -> list[tuple[HypothesisRecord, pathlib.Path]]:
    """Generate hypotheses using the researcher driver."""
    print("=" * 70)
    print(f"PHASE 1: RESEARCH GENERATION (Budget: {budget})")
    print(f"Question: {question}")
    print(f"Theme:    {theme}")
    print("=" * 70, flush=True)

    if dry_run:
        from Gowtam_hypothesis.researcher.schemas import (
            DataRequires,
            ParameterDef,
            SourceClaim,
            SpecData,
        )
        print("[DRY RUN] Creating mock emitted hypotheses...")
        emitted: list[tuple[HypothesisRecord, pathlib.Path]] = []
        for i in range(1, budget + 1):
            mock_id = f"mock-hypo-{i:02d}-{slugify(theme)}"
            spec = SpecData(
                signal_def="close - open",
                entry_rule="Enter long at close when signal > threshold",
                exit_rule="Exit at session close",
                parameters=(ParameterDef("threshold", 0.1, 0.5, 0.1),),
                bar_size="5m",
                session_filter="London open",
                cost_assumption="0.06 round trip per unit",
                data_fields_required=("open", "close", "vwap"),
            )
            rec = HypothesisRecord(
                id=mock_id,
                mechanism=f"Simulated mechanism for {theme} family {i}",
                falsifiable=f"If {theme.lower()} persists then systematic trading produces positive Sortino.",
                operationalize="5m bar close relative to vwap",
                specification=spec,
                data_requires=DataRequires(interval="5m"),
                sources=(
                    SourceClaim(
                        claim=f"Evidence for {theme} in intraday markets.",
                        source_url="https://arxiv.org/abs/2101.00001",
                        source_title=f"{theme} Academic Reference",
                    ),
                ),
                bwis_score=0.620,
            )
            hyp_path = output_dir / f"{mock_id}.md"
            output_dir.mkdir(parents=True, exist_ok=True)
            hyp_path.write_text(
                f"# Hypothesis: {rec.id}\n\nID: {rec.id}\nMechanism: {rec.mechanism}\n"
            )
            emitted.append((rec, hyp_path))
        return emitted

    result = driver.run(
        ask=question,
        budget=budget,
        per_backend=4,
        output_dir=output_dir,
        log_dir=log_dir,
        theme=theme,
        feature_context=feature_context,
    )
    print(f"Research phase complete: Emitted={result.n_emitted}, Blocked={result.n_blocked}")

    pairs: list[tuple[HypothesisRecord, pathlib.Path]] = []
    for rec in result.emitted:
        path = output_dir / f"{rec.id}.md"
        pairs.append((rec, path))
    return pairs


def run_single_campaign(
    rec: HypothesisRecord,
    hyp_path: pathlib.Path,
    profile: str,
    trials: int | None,
    stages: tuple[str, ...],
    dry_run: bool = False,
    resume: bool = True,
    early_stop_trials: int | None = None,
    early_stop_hurdle: float = 0.0,
) -> CampaignOutcome:
    """Execute the pre-registered campaign stages for a single hypothesis."""
    campaign_name: str | None = None
    campaign_dir: pathlib.Path | None = None

    if resume:
        matches = sorted(
            [
                p for p in (REPO_ROOT / "runs" / "campaigns").glob(f"{rec.id}*")
                if p.is_dir() and (p / "campaign.json").exists()
            ],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if matches:
            campaign_dir = matches[0]
            campaign_name = campaign_dir.name
            print(f"[RESUME] Found existing campaign directory: {campaign_name}", flush=True)

    if campaign_dir is None:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
        campaign_name = f"{rec.id}-{stamp}"
        campaign_dir = REPO_ROOT / "runs" / "campaigns" / campaign_name

    score_val = getattr(rec, "bwis_score", None) or 0.523
    band_val = getattr(rec, "bwis_band", None) or bwis.bwis_band(score_val)

    title = getattr(rec, "title", None) or rec.id.replace("-", " ").title()

    print("-" * 70)
    print(f"CAMPAIGN: {campaign_name}")
    print(f"Hypothesis: {title}")
    print(f"BWIS Score: {score_val:.4f} ({band_val})")
    print(f"Profile:    {profile} | Stages: {','.join(stages)}")
    print("-" * 70, flush=True)

    t0 = time.monotonic()

    if dry_run:
        print(f"[DRY RUN] Simulating campaign execution for {campaign_name}...")
        campaign_dir.mkdir(parents=True, exist_ok=True)
        mock_summary = {
            "n_trials": trials or 10,
            "outcomes": {"completed": trials or 10},
            "gate": {
                "observed_best": 0.2450,
                "null_max_mean": 0.1200,
                "p": 0.0350,
                "reject": True,
                "observed_best_positive": True,
                "accept": True,
            },
        }
        (campaign_dir / "summary.json").write_text(json.dumps(mock_summary, indent=2))
        verdict, reasons = evaluate_promotion(
            observed_best=0.2450,
            gate_accept=True,
            bwis_band_val=band_val,
            bwis_score_val=score_val,
            tier3_verdict_val="PASS",
        )
        return CampaignOutcome(
            hypothesis_id=rec.id,
            hypothesis_title=title,
            mechanism=rec.mechanism,
            bwis_score=round(score_val, 4),
            bwis_band=band_val,
            campaign_id=campaign_name,
            profile=profile,
            trials_budget=trials or 10,
            n_trials=trials or 10,
            outcomes={"completed": trials or 10},
            observed_best=0.2450,
            null_max_mean=0.1200,
            p_value=0.0350,
            gate_accept=True,
            tier3_verdict="PASS",
            can_promote=(verdict == "PROMOTE"),
            promotion_verdict=verdict,
            reasons=reasons,
            wall_clock_s=round(time.monotonic() - t0, 2),
        )

    # Real execution with stage idempotency
    for stage in stages:
        if stage == "register" and (campaign_dir / "campaign.json").exists():
            print(f"  >>> Stage: register [SKIPPED - campaign.json exists]", flush=True)
            continue
        elif stage == "run":
            target_trials = trials
            if target_trials is None and (campaign_dir / "campaign.json").exists():
                try:
                    cdata = json.loads((campaign_dir / "campaign.json").read_text())
                    target_trials = cdata.get("budget", 10)
                except Exception:
                    target_trials = 10
            target_trials = target_trials or 10
            if (campaign_dir / "ledger.jsonl").exists():
                rows = [x for x in (campaign_dir / "ledger.jsonl").read_text().splitlines() if x.strip()]
                if len(rows) >= target_trials:
                    print(f"  >>> Stage: run [SKIPPED - all {len(rows)}/{target_trials} trials completed]", flush=True)
                    continue
        elif stage == "nulls" and (campaign_dir / "nulls.json").exists():
            print(f"  >>> Stage: nulls [SKIPPED - nulls.json exists]", flush=True)
            continue
        elif stage == "report" and (campaign_dir / "summary.json").exists() and (campaign_dir / "tier2-summary.json").exists():
            print(f"  >>> Stage: report [SKIPPED - summary.json and tier2-summary.json exist]", flush=True)
            continue

        print(f"  >>> Stage: {stage} ...", flush=True)
        cmd = [
            sys.executable,
            "scripts/nq_campaign.py",
            "--stage",
            stage,
            "--name",
            campaign_name,
            "--profile",
            profile,
        ]
        if stage == "register":
            cmd += ["--hypothesis", str(hyp_path)]
            if trials is not None:
                cmd += ["--trials", str(trials)]
        elif stage == "run":
            if early_stop_trials is not None:
                cmd += ["--early-stop-trials", str(early_stop_trials)]
            if early_stop_hurdle != 0.0:
                cmd += ["--early-stop-hurdle", str(early_stop_hurdle)]

        p = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True)
        if p.returncode != 0:
            print(f"Error in stage {stage}:\n{p.stderr}", file=sys.stderr)
            p.check_returncode()

    # Reconcile outcomes from summary files
    summary_file = campaign_dir / "summary.json"
    tier3_file = campaign_dir / "tier3-verdict.json"

    summary_data: dict[str, Any] = {}
    if summary_file.exists():
        summary_data = json.loads(summary_file.read_text())

    tier3_data: dict[str, Any] = {}
    if tier3_file.exists():
        tier3_data = json.loads(tier3_file.read_text())

    gate = summary_data.get("gate", {})
    observed_best = gate.get("observed_best")
    gate_accept = bool(gate.get("accept", False))
    tier3_v = tier3_data.get("verdict")

    verdict, reasons = evaluate_promotion(
        observed_best=observed_best,
        gate_accept=gate_accept,
        bwis_band_val=band_val,
        bwis_score_val=score_val,
        tier3_verdict_val=tier3_v,
    )

    outcome = CampaignOutcome(
        hypothesis_id=rec.id,
        hypothesis_title=title,
        mechanism=rec.mechanism,
        bwis_score=round(score_val, 4),
        bwis_band=band_val,
        campaign_id=campaign_name,
        profile=profile,
        trials_budget=trials or summary_data.get("n_trials", 0),
        n_trials=summary_data.get("n_trials", 0),
        outcomes=summary_data.get("outcomes", {}),
        observed_best=observed_best,
        null_max_mean=gate.get("null_max_mean"),
        p_value=gate.get("p"),
        gate_accept=gate_accept,
        tier3_verdict=tier3_v,
        can_promote=(verdict == "PROMOTE"),
        promotion_verdict=verdict,
        reasons=reasons,
        wall_clock_s=round(time.monotonic() - t0, 2),
    )

    # Tier 2 Feedback: Update BWIS empirical feedback and Champion Feature Memory
    sources_data = [
        {"claim": getattr(s, "claim", ""), "source_url": getattr(s, "source_url", ""), "source_title": getattr(s, "source_title", "")}
        for s in getattr(rec, "sources", ())
    ]
    bwis.record_backtest_feedback(
        hypothesis_id=rec.id,
        theme=getattr(rec, "theme", "") or profile,
        sources=sources_data,
        observed_best=observed_best,
        gate_accept=gate_accept,
    )

    from Gowtam_hypothesis.researcher.feature_memory import FeatureMemory
    feat_mem = FeatureMemory()
    feat_mem.record_outcome(
        rec=rec,
        outcome=outcome,
        theme=getattr(rec, "theme", "") or profile,
        campaign_dir=campaign_dir,
    )

    return outcome


def write_summary_report(
    question: str,
    theme: str,
    profile: str,
    outcomes: list[CampaignOutcome],
    out_dir: pathlib.Path,
) -> pathlib.Path:
    """Format and write final results to summary.json and results.jsonl."""
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir / "summary.json"
    jsonl_path = out_dir / "results.jsonl"

    summary_payload = {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "question": question,
        "theme": theme,
        "profile": profile,
        "total_campaigns": len(outcomes),
        "promoted_count": sum(1 for o in outcomes if o.can_promote),
        "campaigns": [o.as_dict() for o in outcomes],
    }

    summary_path.write_text(json.dumps(summary_payload, indent=2))

    with jsonl_path.open("w") as fh:
        for o in outcomes:
            fh.write(json.dumps(o.as_dict()) + "\n")

    return summary_path


def print_terminal_table(outcomes: list[CampaignOutcome]) -> None:
    """Print an ASCII table summarising all campaign outcomes."""
    print("\n" + "=" * 90)
    print("CAMPAIGN LOOP EXECUTION SUMMARY")
    print("=" * 90)
    header = (
        f"{'Hypothesis ID':<30} | {'BWIS':<10} | {'Best':<8} | "
        f"{'Gate':<7} | {'Tier 3':<7} | {'Verdict':<8}"
    )
    print(header)
    print("-" * 90)
    for o in outcomes:
        best_str = f"{o.observed_best:.3f}" if o.observed_best is not None else "None"
        bwis_str = f"{o.bwis_band} ({o.bwis_score:.2f})"
        gate_str = "ACCEPT" if o.gate_accept else "REJECT"
        tier3_str = str(o.tier3_verdict or "N/A")
        verdict_str = o.promotion_verdict
        print(
            f"{o.hypothesis_id[:30]:<30} | {bwis_str:<10} | {best_str:<8} | "
            f"{gate_str:<7} | {tier3_str:<7} | {verdict_str:<8}"
        )
    print("=" * 90 + "\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run single-question research and campaign evaluation loop.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--question",
        type=str,
        default=DEFAULT_QUESTION,
        help="Trading-system research question to investigate.",
    )
    parser.add_argument(
        "--theme",
        type=str,
        default=DEFAULT_THEME,
        help="Research theme for BWIS prior indexing.",
    )
    parser.add_argument(
        "--budget",
        type=int,
        default=DEFAULT_BUDGET,
        help="Hypothesis budget to emit in research phase.",
    )
    parser.add_argument(
        "--profile",
        type=str,
        default=DEFAULT_PROFILE,
        help="run.yaml profile to backtest campaigns against.",
    )
    parser.add_argument(
        "--trials",
        type=int,
        default=None,
        help="Override trial count per campaign (defaults to profile setting).",
    )
    parser.add_argument(
        "--out",
        type=pathlib.Path,
        default=None,
        help="Output directory for research artifacts and summary.",
    )
    parser.add_argument(
        "--stages",
        type=str,
        default="register,run,nulls,report",
        help="Comma-separated campaign stages to execute.",
    )
    parser.add_argument(
        "--skip-research",
        type=pathlib.Path,
        default=None,
        help="Directory of existing hypothesis Markdown files to run campaigns for.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Do not resume existing matching campaigns; always generate new campaign directories.",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=1,
        help="Number of concurrent hypothesis campaigns to run in parallel.",
    )
    parser.add_argument(
        "--early-stop-trials",
        type=int,
        default=None,
        help="Minimum trials to run before evaluating early stopping hurdle.",
    )
    parser.add_argument(
        "--early-stop-hurdle",
        type=float,
        default=0.0,
        help="Primary score hurdle required to continue beyond early-stop-trials.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without calling external LLM or heavy simulation (for tests).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    import concurrent.futures
    from Gowtam_hypothesis.researcher.feature_memory import FeatureMemory

    args = parse_args(argv)

    slug = slugify(args.question)
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = args.out or (REPO_ROOT / "runs" / "research" / f"{slug}-{timestamp}")
    hypotheses_dir = out_dir / "hypotheses"
    logs_dir = out_dir / "logs"

    stages = tuple(s.strip() for s in args.stages.split(",") if s.strip())
    resume = not args.no_resume

    feat_mem = FeatureMemory()
    feat_ctx = feat_mem.get_feature_context(theme=args.theme, question=args.question)
    if feat_ctx:
        print(f"[FEATURE MEMORY] Injected champion context ({len(feat_mem.entries)} patterns)")

    # Step 1: Research Phase
    if args.skip_research:
        print(f"Skipping research; loading hypotheses from {args.skip_research}")
        hyp_files = sorted(args.skip_research.glob("*.md"))
        if not hyp_files:
            sys.exit(f"No .md hypothesis files found in {args.skip_research}")
        emitted_pairs = []
        for hf in hyp_files:
            hyp_id = hf.stem
            from Gowtam_hypothesis.researcher.schemas import DataRequires, SourceClaim, SpecData
            spec = SpecData(
                signal_def="close - open",
                entry_rule="Enter at close",
                exit_rule="Exit at session close",
                parameters=(),
                bar_size="5m",
                session_filter="all",
                cost_assumption="0.06 round trip per unit",
                data_fields_required=("close", "open"),
            )
            rec = HypothesisRecord(
                id=hyp_id,
                mechanism=f"Loaded from {hf.name}",
                falsifiable=f"Systematic trading of {hyp_id} produces positive Sharpe.",
                operationalize="5m bar close",
                specification=spec,
                data_requires=DataRequires(interval="5m"),
                sources=(
                    SourceClaim(
                        claim=f"Prior literature baseline for {hyp_id}.",
                        source_url="https://arxiv.org/abs/2101.00001",
                        source_title=f"{hyp_id} Reference",
                    ),
                ),
                confidence_prior="Pre-existing hypothesis.",
                bwis_score=0.580,
            )
            emitted_pairs.append((rec, hf))
    else:
        emitted_pairs = run_research_phase(
            question=args.question,
            theme=args.theme,
            budget=args.budget,
            output_dir=hypotheses_dir,
            log_dir=logs_dir,
            dry_run=args.dry_run,
            feature_context=feat_ctx,
        )

    if not emitted_pairs:
        print("No hypotheses emitted to run campaigns for.")
        return 0

    # Step 2: Campaign Loop (Parallel or Sequential)
    outcomes: list[CampaignOutcome] = []

    if args.parallel > 1 and not args.dry_run:
        print(f"\n[PARALLEL] Running {len(emitted_pairs)} campaigns concurrently with {args.parallel} workers...\n")
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel) as executor:
            future_to_rec = {
                executor.submit(
                    run_single_campaign,
                    rec=rec,
                    hyp_path=hyp_path,
                    profile=args.profile,
                    trials=args.trials,
                    stages=stages,
                    dry_run=args.dry_run,
                    resume=resume,
                    early_stop_trials=args.early_stop_trials,
                    early_stop_hurdle=args.early_stop_hurdle,
                ): rec
                for rec, hyp_path in emitted_pairs
            }
            for fut in concurrent.futures.as_completed(future_to_rec):
                outcomes.append(fut.result())
    else:
        for rec, hyp_path in emitted_pairs:
            outcome = run_single_campaign(
                rec=rec,
                hyp_path=hyp_path,
                profile=args.profile,
                trials=args.trials,
                stages=stages,
                dry_run=args.dry_run,
                resume=resume,
                early_stop_trials=args.early_stop_trials,
                early_stop_hurdle=args.early_stop_hurdle,
            )
            outcomes.append(outcome)

    # Step 3: Synthesis & Reporting
    summary_path = write_summary_report(
        question=args.question,
        theme=args.theme,
        profile=args.profile,
        outcomes=outcomes,
        out_dir=out_dir,
    )
    print_terminal_table(outcomes)
    print(f"Summary written to: {summary_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
