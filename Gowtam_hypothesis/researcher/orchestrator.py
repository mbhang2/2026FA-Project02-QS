"""The orchestration loop: researcher -> campaigns -> decide the next direction.

This is the OUTER loop around the researcher. It does **not** re-implement the
campaign and it does **not** declare winners -- the gate does both. Its only
jobs are (1) run hypotheses through the existing campaign, (2) read back a
*whitelisted* trajectory for each, and (3) ask the orchestrator pass what to
research next.

The whitelist is the load-bearing part and it copies `src.core.loop.llm`'s
`_history_block` exactly in spirit: the orchestrator is shown the direction the
score moved, trade counts, and turnover -- never a p-value, an accept/reject
verdict, N, or anything the correction computes. An orchestrator shown the gate
would optimize the gate; that is the one bug this project exists to prevent, one
level up.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import generate, llm
from .llm import LLMError, ModelSubstituted

REPO_ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATE_MODEL = "claude-haiku-4-5"

# Bounds at module top (L1 style): the loop terminates loudly, never hangs.
MAX_CAMPAIGNS_PER_LOOP = 5
WALL_CLOCK_S = 6 * 3600  # a campaign is ~3 hours; two is the practical ceiling

# The run.yaml profile the loop searches on. XAUUSD spot, not NQ-from-R2: the
# dataset is local Parquet and the profile carries the quotes fill model +
# commission convention. This is a research choice, stated here rather than
# buried in a subprocess call.
PROFILE = "xauusd-train"
CAMPAIGN_STAGES = ("register", "run", "nulls", "report")


def _python(cwd: Path) -> str:
    """The interpreter that runs the campaign script.

    Resolved from `cwd` rather than `sys.executable`: the researcher may itself
    be running under a thin interpreter, and the campaign needs the repo's venv
    (yaml, pandas, pyarrow) which lives at `.venv/`. Falls back only if absent.
    """
    venv = cwd / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


class TrajectoryLeak(RuntimeError):
    """A trajectory row carried a field the orchestrator may never see."""

    # The ONLY fields the orchestrator is shown. This is a whitelist, not a
    # blacklist: a blacklist over a growing row shape leaks the first field
    # somebody adds. Same argument as `llm._history_block`.
    ALLOWED = {"outcome", "trades", "sessions", "score_dir", "turnover_dir"}


@dataclass(frozen=True, slots=True)
class Trajectory:
    outcome: str
    trades: int | None = None
    sessions: int | None = None
    score_dir: str | None = None      # "up" | "down" | "flat" -- direction only
    turnover_dir: str | None = None   # "up" | "down" | "flat"


@dataclass(frozen=True, slots=True)
class OrchestrationDecision:
    next_ask: str
    why: str
    families_to_drop: tuple[str, ...] = ()
    families_to_deepen: tuple[str, ...] = ()


@dataclass
class LoopResult:
    iterations: int = 0
    decisions: list[OrchestrationDecision] = field(default_factory=list)
    log_path: Path | None = None


def to_trajectory(row: dict) -> Trajectory:
    """Whitelist a campaign row into a Trajectory, refusing unknown fields."""
    leaked = [k for k in row if k not in TrajectoryLeak.ALLOWED]
    if leaked:
        raise TrajectoryLeak(
            f"campaign row carried fields the orchestrator may not see: {leaked}")
    return Trajectory(
        outcome=str(row.get("outcome", "")),
        trades=row.get("trades"),
        sessions=row.get("sessions"),
        score_dir=row.get("score_dir"),
        turnover_dir=row.get("turnover_dir"),
    )


def orchestrate(trajectories: list[Trajectory]) -> OrchestrationDecision:
    """One gateway pass over whitelisted trajectories -> the next direction.

    The prompt states, and the input enforces, that no gate field is present.
    """
    rows = [json.dumps(
        {"outcome": t.outcome, "trades": t.trades, "sessions": t.sessions,
         "score_dir": t.score_dir, "turnover_dir": t.turnover_dir})
        for t in trajectories]
    user = (
        "You direct the next step of a quant research loop. You are shown only "
        "the TRAJECTORY of past campaigns (score direction, trade count, "
        "turnover), never any significance measure, threshold, accept/reject "
        "verdict, or count of trials. If any such field appears, stop and say so.\n\n"
        "Decide on the basis of what trajectories honestly suggest: which "
        "mechanism family to deepen, which to drop, and a genuinely new next "
        "direction. Do NOT declare any result a winner. Return JSON:\n"
        '{"next_ask":"one-line direction","why":"...",'
        '"families_to_drop":[],"families_to_deepen":[]}\n\n'
        "Trajectories:\n" + "\n".join(rows)
    )
    try:
        comp = llm.call(ORCHESTRATE_MODEL, [
            {"role": "system",
             "content": "You decide research direction from trajectories only. "
                        "Never infer a significance measure. Return JSON only."},
            {"role": "user", "content": user},
        ], temperature=0.3, max_tokens=1024)
    except (LLMError, ModelSubstituted) as exc:
        raise generate.PassBlocked(f"orchestrate call failed: {exc}") from None

    data = llm.extract_json(comp.text)
    if data is None:
        raise generate.PassBlocked("orchestrate returned no JSON")
    return OrchestrationDecision(
        next_ask=str(data.get("next_ask", "")).strip(),
        why=str(data.get("why", "")).strip(),
        families_to_drop=tuple(str(x) for x in (data.get("families_to_drop") or [])),
        families_to_deepen=tuple(str(x) for x in (data.get("families_to_deepen") or [])),
    )


def run_campaign(campaign_name: str, *, cwd: Path = REPO_ROOT,
                 dry_run: bool = False, profile: str = PROFILE) -> None:
    """Run the committed campaign flow for one emitted hypothesis.

    Not re-implemented here: the campaign script owns register/run/nulls/report
    and the gate lives in `report`. This seam only passes the hypothesis the
    researcher wrote (`config/hypotheses/<id>.md`) into the script's `--hypothesis`
    and the profile choice, then runs the four stages in order. `dry_run=True`
    prints the commands instead of executing them.
    """
    hyp = REPO_ROOT / "config" / "hypotheses" / f"{campaign_name}.md"
    if not hyp.exists():
        raise FileNotFoundError(
            f"no emitted hypothesis at {hyp}; the researcher must write it "
            "before the campaign can lock it in.")
    for stage in CAMPAIGN_STAGES:
        cmd = [_python(cwd), "scripts/nq_campaign.py", "--name", campaign_name,
               "--profile", profile, "--stage", stage]
        if stage == "register":
            cmd += ["--hypothesis", str(hyp)]
        if dry_run:
            print(f"[dry-run] {campaign_name} [{profile}]: {' '.join(cmd)}",
                  flush=True)
            continue
        subprocess.run(cmd, cwd=cwd, check=True)  # noqa: S603


def read_trajectory(name: str, *, cwd: Path = REPO_ROOT) -> Trajectory:
    """Whitelist a campaign's ledger into a `Trajectory`, showing only direction.

    Reads the champion row -- the highest non-rejected primary -- and maps it to
    the whitelisted fields. `score_dir` is the sign of that score, never its
    magnitude; no p-value, accept/reject verdict, N, or null statistic is read,
    for the reason `TrajectoryLeak` exists.
    """
    ledger = cwd / "runs" / "campaigns" / name / "ledger.jsonl"
    if not ledger.exists():
        return Trajectory(outcome="none")
    rows = [json.loads(x) for x in ledger.read_text().splitlines() if x.strip()]
    if not rows:
        return Trajectory(outcome="none")
    best, best_score = None, None
    for r in rows:
        s = r.get("score") or {}
        if s.get("rejected") or s.get("primary") is None:
            continue
        if best_score is None or s["primary"] > best_score:
            best, best_score = r, s["primary"]
    if best is None:
        return Trajectory(outcome=str(rows[0].get("outcome", "none")))
    s = best.get("score") or {}
    return Trajectory(
        outcome=str(best.get("outcome", "")),
        trades=int(s["trade_count"]) if s.get("trade_count") is not None else None,
        sessions=int(s["n_sessions"]) if s.get("n_sessions") is not None else None,
        score_dir="up" if best_score > 0 else ("down" if best_score < 0 else "flat"),
        turnover_dir=None,
    )


def run_loop(ask: str, *, emit_fn, campaign_fn=run_campaign, max_iterations: int = 2,
             dry_run: bool = True, log_dir: Path | None = None) -> LoopResult:
    """The outer loop. `emit_fn(ask)` from the researcher; `campaign_fn` runs one.

    `emit_fn` and `campaign_fn` are seams so the loop is testable offline without
    a real researcher batch or a 3-hour campaign.
    """
    result = LoopResult()
    deadline = time.monotonic() + WALL_CLOCK_S
    current_ask = ask
    trajectories: list[Trajectory] = []

    for _ in range(max_iterations):
        if time.monotonic() > deadline:
            break
        emitted = emit_fn(current_ask)
        for rec in emitted[:MAX_CAMPAIGNS_PER_LOOP]:
            campaign_fn(rec.id, dry_run=dry_run)
            if dry_run:
                trajectories.append(Trajectory(outcome="dry_run", score_dir="flat"))
            else:
                trajectories.append(read_trajectory(rec.id))
        decision = orchestrate(trajectories)
        result.decisions.append(decision)
        if not decision.next_ask.strip():
            break
        current_ask = decision.next_ask
        result.iterations += 1

    return result
