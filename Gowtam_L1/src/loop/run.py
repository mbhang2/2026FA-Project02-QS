"""Lightweight runner for testing hypotheses through the L1 loop + engine.

Usage (from the project root, ``Gowtam_L1/``, with ``.venv/bin/python``):

    python -m src.loop.run --hypothesis "20-day momentum: long/flat"
    python -m src.loop.run --hypothesis-file hypotheses/example_momentum.md
    cat hypotheses/x.md | python -m src.loop.run --stdin
    python -m src.loop.run --list

Runs the full pipeline (planner -> worker -> reviewer -> conformance -> engine ->
package) in one shot, and prints a compact summary. Reusing an explicit
``--name`` resumes; otherwise a timestamped id is auto-generated so re-runs never
clash.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from src.loop import paths
from src.loop.auditor_step import run_auditor_step
from src.loop.committee_step import run_committee_step
from src.loop.driver import run_candidate
from src.loop.reproducibility import capture_reproducibility
from src.loop.statistician_step import run_statistician_step
from src.loop.state_store import load_state, save_state
from src.packaging.package_candidate import (
    EngineRunError,
    assemble_package,
    run_engine_for_candidate,
    verify_strategy_conformance,
)
from src.schemas import ApprovedCandidate, CandidatePackage

# --- defaults tuned for the placeholder dataset (asset_a/asset_b, ~120 bars) ---
DEFAULT_ASSETS = ["asset_a"]
DEFAULT_START = "2024-01-01"
DEFAULT_TRAIN_END = "2024-02-01"
DEFAULT_VAL_END = "2024-03-01"
DEFAULT_END = "2024-04-15"


def _slug(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s[:max_len] or "hypothesis"


def auto_candidate_id(hypothesis: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return f"{_slug(hypothesis)}_{stamp}"


def run_full(candidate: ApprovedCandidate) -> CandidatePackage:
    """Run the fully-agentic staged pipeline to a committee PASS/VETO + package.

    Stages: planner -> worker -> reviewer (build loop, in ``run_candidate``) ->
    conformance -> engine -> statistician -> auditor -> committee -> package.
    There is no human stage; the committee's PASS/VETO is the binding outcome.
    """
    cid = candidate.candidate_id
    state = run_candidate(candidate)

    if state.status == "FAILED":
        return assemble_package(cid)

    if not verify_strategy_conformance(cid):
        state.status = "FAILED"
        state.failure_reason = "strategy_non_conforming"
        save_state(state)
        return assemble_package(cid)

    try:
        run_engine_for_candidate(candidate)
    except EngineRunError as exc:
        state.status = "FAILED"
        state.failure_reason = str(exc)
        save_state(state)
        return assemble_package(cid)

    # Staged agentic post-engine committee (B.4): read-only statistician and
    # auditor, then the judge that aggregates them into a binding PASS/VETO.
    run_statistician_step(candidate)
    run_auditor_step(candidate)
    verdict = run_committee_step(candidate)

    _write_reproducibility(candidate)

    if verdict.verdict == "VETO":
        state.status = "FAILED"
        state.failure_reason = f"committee_veto: {verdict.reasoning}"
        save_state(state)
        return assemble_package(cid)

    state.status = "PACKAGED"
    save_state(state)
    return assemble_package(cid)


def _write_reproducibility(candidate: ApprovedCandidate) -> None:
    """Record the reproducibility block (B.5) and attach it to the trial log."""
    cid = candidate.candidate_id
    repro = capture_reproducibility(candidate)
    paths.candidate_file(cid, "reproducibility.json").write_text(
        json.dumps(repro, indent=2)
    )
    tl_path = paths.candidate_file(cid, "trial_log.json")
    if tl_path.exists():
        record = json.loads(tl_path.read_text())
        record["reproducibility"] = repro
        tl_path.write_text(json.dumps(record, indent=2))


def summarize(candidate_id: str) -> dict:
    """Pull a compact summary of one candidate for display."""
    state = load_state(candidate_id)
    summary = {
        "candidate_id": candidate_id,
        "status": state.status if state else "?",
        "final_status": "-",
        "composite": None,
        "val_sharpe": None,
        "holdout_sharpe": None,
        "decay": None,
    }
    pkg_path = paths.candidate_file(candidate_id, "package.json")
    if pkg_path.exists():
        pkg = json.loads(pkg_path.read_text())
        summary["final_status"] = pkg.get("final_status", "-")
        trial = pkg.get("trial_log") or {}
        result = trial.get("result") or {}
        summary["composite"] = result.get("composite_score")
        summary["val_sharpe"] = (result.get("validation_metrics") or {}).get("sharpe_ratio")
        summary["holdout_sharpe"] = (result.get("holdout_metrics") or {}).get("sharpe_ratio")
        summary["decay"] = (result.get("decay") or {}).get("decay_flag")
    return summary


def _fmt(x):
    return "-" if x is None else f"{x:.3f}"


def _print_summary(s: dict) -> None:
    print(
        f"{s['candidate_id']}\n"
        f"  status={s['status']} final={s['final_status']} "
        f"composite={_fmt(s['composite'])} "
        f"val_sharpe={_fmt(s['val_sharpe'])} "
        f"holdout={_fmt(s['holdout_sharpe'])} decay={s['decay']}"
    )


def _load_hypothesis(args) -> str:
    if getattr(args, "stdin", False):
        text = sys.stdin.read().strip()
    elif getattr(args, "hypothesis_file", None):
        text = Path(args.hypothesis_file).read_text().strip()
    else:
        text = args.hypothesis or ""
    if not text:
        sys.exit("error: empty hypothesis")
    return text


def cmd_run(args) -> None:
    hypothesis = _load_hypothesis(args)
    cid = args.name or auto_candidate_id(hypothesis)
    candidate = ApprovedCandidate(
        candidate_id=cid,
        hypothesis=hypothesis,
        source_paper_ref=args.paper_ref,
        asset_ids=args.assets,
        start=args.start,
        train_end=args.train_end,
        val_end=args.val_end,
        end=args.end,
    )
    print(f"\nRunning candidate: {cid}")

    if args.skip_engine:
        state = run_candidate(candidate)
        print(f"Loop status: {state.status}"
              + (f" (failure={state.failure_reason})" if state.failure_reason else ""))
        print("Built file:", paths.candidate_file(cid, paths.STRATEGY_FILE))
        return

    package = run_full(candidate)
    _print_summary(summarize(cid))
    print("Package:", paths.candidate_file(cid, "package.json"))


def cmd_list(args) -> None:
    root = paths.PROJECT_ROOT / "candidates"
    if not root.exists():
        print("no candidates yet")
        return
    rows = sorted(d.name for d in root.iterdir() if d.is_dir())
    if not rows:
        print("no candidates yet")
        return
    print(f"{'id':<42} {'status':<12} {'final':<20} {'composite':>10} {'val':>8} {'hold':>8} {'decay':>6}")
    for cid in rows:
        s = summarize(cid)
        print(
            f"{cid:<42} {s['status']:<12} {s['final_status']:<20} "
            f"{_fmt(s['composite']):>10} {_fmt(s['val_sharpe']):>8} "
            f"{_fmt(s['holdout_sharpe']):>8} {str(s['decay']):>6}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="src.loop.run", description="Run hypotheses through the L1 loop.")
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--hypothesis", help="one-line hypothesis string")
    src.add_argument("--hypothesis-file", help="path to a multi-line hypothesis file")
    src.add_argument("--stdin", action="store_true", help="read hypothesis from stdin")
    parser.add_argument("--list", action="store_true", help="compare all past candidates")
    parser.add_argument("--name", help="candidate id (reuse to resume); default auto-generated")
    parser.add_argument("--assets", nargs="*", default=DEFAULT_ASSETS)
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--train-end", default=DEFAULT_TRAIN_END)
    parser.add_argument("--val-end", default=DEFAULT_VAL_END)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--paper-ref", default=None)
    parser.add_argument("--skip-engine", action="store_true", help="run loop only, skip engine/package")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.list:
        cmd_list(args)
        return
    if not (args.hypothesis or args.hypothesis_file or args.stdin):
        build_parser().print_help()
        sys.exit("error: provide --hypothesis, --hypothesis-file, --stdin, or --list")
    cmd_run(args)


if __name__ == "__main__":
    main()
