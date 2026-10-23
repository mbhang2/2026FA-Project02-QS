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
    #
    # bwis_band is a directional string ("high"/"mid"/"low"); the raw float
    # is never passed through so the orchestrator cannot optimise against the
    # calibration directly.
    ALLOWED = {"outcome", "trades", "sessions", "score_dir", "turnover_dir", "bwis_band"}


@dataclass(frozen=True, slots=True)
class Trajectory:
    outcome: str
    trades: int | None = None
    sessions: int | None = None
    score_dir: str | None = None      # "up" | "down" | "flat" -- direction only
    turnover_dir: str | None = None   # "up" | "down" | "flat"
    bwis_band: str | None = None      # "high" | "mid" | "low" -- source quality band


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
        bwis_band=row.get("bwis_band"),
    )


def orchestrate(trajectories: list[Trajectory]) -> OrchestrationDecision:
    """One gateway pass over whitelisted trajectories -> the next direction.

    The prompt states, and the input enforces, that no gate field is present.
    The pass runs at temperature 0 -- it is a decision, not a brainstorm -- and
    retries once with an explicit "JSON only" instruction, because a single
    malformed reply from a stochastic model must not cost a six-hour loop its
    next direction.
    """
    rows = [json.dumps(
        {"outcome": t.outcome, "trades": t.trades, "sessions": t.sessions,
         "score_dir": t.score_dir, "turnover_dir": t.turnover_dir,
         "bwis_band": t.bwis_band})
        for t in trajectories]
    user = (
        "You direct the next step of a quant research loop. You are shown only "
        "the TRAJECTORY of past campaigns (score direction, trade count, "
        "turnover, and bwis_band), never any significance measure, threshold, accept/reject "
        "verdict, or count of trials. If any such field appears, stop and say so.\n\n"
        "bwis_band reflects the quality of cited sources: 'high' means institutional "
        "or strong academic sources anchored the hypothesis; 'low' means sources were "
        "weak or off-topic. Prefer deepening themes where bwis_band is high; flag "
        "themes where it is consistently 'low' as needing better source coverage "
        "before hypothesising further.\n\n"
        "Decide on the basis of what trajectories honestly suggest: which "
        "mechanism family to deepen, which to drop, and a genuinely new next "
        "direction. Do NOT declare any result a winner. Return JSON:\n"
        '{"next_ask":"one-line direction","why":"...",'
        '"families_to_drop":[],"families_to_deepen":[]}\n\n'
        "Trajectories:\n" + "\n".join(rows)
    )
    system = ("You decide research direction from trajectories only. "
              "Never infer a significance measure. Return JSON only.")
    data = None
    last_reply = ""
    for attempt in (0, 1):
        prompt = user if attempt == 0 else (
            user + "\n\nReply with the JSON object only, on one line, no "
            "surrounding text or code fence.")
        try:
            comp = llm.call(ORCHESTRATE_MODEL, [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ], temperature=0.0, max_tokens=1024)
        except ModelSubstituted as exc:
            raise generate.PassBlocked(
                f"orchestrate model mismatch: {exc}; asked {ORCHESTRATE_MODEL!r}")
        except LLMError as exc:
            # An HTTP/transport failure must surface loudly -- status, endpoint,
            # model, provider -- never as a misleading "no JSON". The message
            # never contains the API key.
            detail = (
                f"orchestrate LLM request failed:\n"
                f"  HTTP {exc.status if exc.status is not None else '?'}\n"
                f"  model={exc.model or ORCHESTRATE_MODEL}\n"
                f"  endpoint={exc.endpoint}\n"
                f"  provider={exc.provider}\n"
                f"  content-type={exc.content_type or '(none)'}"
            )
            if exc.body:
                detail += f"\n  body={exc.body!r}"
            raise generate.PassBlocked(detail) from None
        data = llm.extract_json(comp.text)
        if data is not None:
            break
        last_reply = comp.text
    if data is None:
        raise generate.PassBlocked(
            "orchestrate returned no parseable JSON after one retry; "
            f"last reply={last_reply[:300]!r}")
    return OrchestrationDecision(
        next_ask=str(data.get("next_ask", "")).strip(),
        why=str(data.get("why", "")).strip(),
        families_to_drop=tuple(str(x) for x in (data.get("families_to_drop") or [])),
        families_to_deepen=tuple(str(x) for x in (data.get("families_to_deepen") or [])),
    )


def run_campaign(campaign_name: str, *, cwd: Path = REPO_ROOT,
                 dry_run: bool = False, profile: str = PROFILE,
                 hypothesis_id: str | None = None) -> None:
    """Run the committed campaign flow for one emitted hypothesis.

    Not re-implemented here: the campaign script owns register/run/nulls/report
    and the gate lives in `report`. This seam only passes the hypothesis the
    researcher wrote (`config/hypotheses/<id>.md`) into the script's `--hypothesis`
    and the profile choice, then runs the four stages in order. `dry_run=True`
    prints the commands instead of executing them.

    `campaign_name` names the pre-registration directory; `hypothesis_id` names
    the hypothesis file (defaults to `campaign_name`), so a timestamped campaign
    name can still read back the un-stamped hypothesis markdown.
    """
    hid = hypothesis_id or campaign_name
    hyp = REPO_ROOT / "config" / "hypotheses" / f"{hid}.md"
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
             dry_run: bool = True, log_dir: Path | None = None,
             trajectory_fn=None) -> LoopResult:
    """The outer loop. `emit_fn(ask)` from the researcher; `campaign_fn` runs one.

    `emit_fn`, `campaign_fn`, and `trajectory_fn` are seams so the loop is
    testable offline without a real researcher batch or a 3-hour campaign.
    `trajectory_fn(name)` maps a hypothesis id to the campaign name under which
    its trajectory was written (default: the id itself).
    """
    result = LoopResult()
    deadline = time.monotonic() + WALL_CLOCK_S
    current_ask = ask
    trajectories: list[Trajectory] = []

    for _ in range(max_iterations):
        if time.monotonic() > deadline:
            break
        try:
            emitted = emit_fn(current_ask)
        except (generate.PassBlocked, LLMError, ModelSubstituted) as exc:
            # A blocked emit ends the loop rather than crashing it: the research
            # and campaigns up to here are already recorded, and a terminally
            # non-responsive model is not a direction.
            print(f"emit pass failed ({type(exc).__name__}: {exc}); "
                  "stopping the loop", flush=True)
            break
        for rec in emitted[:MAX_CAMPAIGNS_PER_LOOP]:
            campaign_fn(rec.id, dry_run=dry_run)
            if dry_run:
                trajectories.append(Trajectory(outcome="dry_run", score_dir="flat"))
            else:
                name = trajectory_fn(rec.id) if trajectory_fn else rec.id
                trajectories.append(read_trajectory(name))
        if not trajectories:
            print("No hypotheses were successfully emitted/run; stopping the loop", flush=True)
            break

        try:
            decision = orchestrate(trajectories)
        except generate.PassBlocked as exc:
            # A blocked orchestrate pass ends the loop rather than crashing it:
            # the research and campaigns up to here are already recorded, and a
            # terminally non-responsive model is not a direction.
            print(f"orchestrate pass blocked ({exc}); stopping the loop", flush=True)
            break
        result.decisions.append(decision)
        if not decision.next_ask.strip():
            break
        current_ask = decision.next_ask
        result.iterations += 1

    return result


def full_run(ask: str, *, budget: int = 1, profile: str = PROFILE,
             max_iterations: int = 2, output_dir: Path | None = None,
             dry_run: bool = False) -> LoopResult:
    """One command: research the ask, then run the L1 loop through to report.

    The single entry point that chains the researcher and the campaign loop
    into one run:

    1. ``driver.run`` decomposes the ask and emits up to ``budget`` hypotheses
       into ``config/hypotheses/``.
    2. Each emitted hypothesis is run through the full campaign sequence
       (register -> run -> nulls -> report) under a unique, timestamped
       campaign name so a re-run never collides with a prior pre-registration.
    3. The whitelisted trajectory is read back and fed to the orchestration
       pass, which names the next direction; the loop repeats.

    This is ``run_loop`` with a live, non-colliding ``emit_fn``/``campaign_fn``
    bound by default. It is NOT a dry run: campaigns execute for real.
    """
    from . import driver

    stamp = time.strftime("%Y%m%d-%H%M%S")

    def emit(ask_: str):
        return driver.run(ask_, budget=budget, output_dir=output_dir).emitted

    def campaign(name: str, *, dry_run: bool = False):
        # A unique per-run name: the researcher may emit the same hypothesis id
        # across iterations, and `register` refuses to overwrite. The timestamp
        # suffix keeps each live campaign its own pre-registration, while the
        # hypothesis file on disk stays keyed by the un-stamped id.
        run_campaign(f"{name}-{stamp}", profile=profile, dry_run=dry_run,
                     hypothesis_id=name)

    def trajectory(name: str) -> Trajectory:
        return read_trajectory(f"{name}-{stamp}")

    # Re-bind the loop against the stamped campaign naming, live.
    result = run_loop(
        ask,
        emit_fn=emit,
        campaign_fn=campaign,
        max_iterations=max_iterations,
        dry_run=dry_run,
        trajectory_fn=trajectory,
    )
    # The stamped trajectories are what a caller (or a human) reads back.
    return result
