"""A campaign: a budget of trials under one id, and the correction over them.

**One call is one trial.** The trial itself is `run.py`'s path, unchanged: an
isolated subprocess under a committed profile, one ledger row per invocation
whatever the outcome. The campaign wraps that, gives every row a campaign id, and
applies the correction at the end.

**The campaign record is written and hashed before the campaign runs.** Budget,
stopping rule, q, which correction, the stub's seed. A budget or a q chosen after
seeing results is the failure this whole project exists to prevent, and building
the loop without that guard would have been an embarrassing place to commit it.
The record is re-hashed at the end and a mismatch is reported loudly.

**N is the count of the campaign's trial rows -- accepted, rejected and crashed
alike.** No filtering. A crashed trial is a trial that was run and looked at, and
dropping it would shrink the denominator by exactly the trials that did not work.
The single exception is `harness_failed`, which is not a trial at all: it is the
engine, the pipe or the platform failing rather than the candidate, and Task 25
gave it its own outcome for this reason. Excluded, counted separately, and
visible in the summary.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[3]

# Outcomes that are trials. `harness_failed` is deliberately absent.
TRIAL_OUTCOMES = ("completed", "crashed", "abandoned_on_budget",
                  "invalidated_scoring_mutated", "invalidated_artifacts_mutated",
                  "invalidated_fill_log_mismatch", "refused")


class CampaignTampered(RuntimeError):
    """The campaign record changed between being written and being read back."""


@dataclass
class CampaignRecord:
    """Pre-registered, before the first trial runs."""

    campaign_id: str
    profile: str
    budget: int
    q: float
    corrections: tuple[str, ...] = ("benjamini_hochberg", "holm", "expected_max")
    stopping_rule: str = "fixed budget; no early stop on any observed result"
    agent: str = ""
    agent_seed: int = 0
    agent_crash_fraction: float = 0.0
    dataset: str = ""
    split: str = ""
    note: str = ""
    written_at: str = ""

    def digest(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()

    def write(self, path: Path) -> str:
        """Write, hash, return the digest. The digest is what gets checked."""
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {"record": asdict(self)}
        body["digest"] = self.digest()
        path.write_text(json.dumps(body, indent=2, sort_keys=True, default=str) + "\n")
        return body["digest"]

    @classmethod
    def read(cls, path: Path) -> tuple["CampaignRecord", str]:
        body = json.loads(path.read_text())
        record = cls(**body["record"])
        stored = body["digest"]
        if record.digest() != stored:
            raise CampaignTampered(
                f"{path} does not hash to what it says: stored {stored[:12]}, "
                f"recomputed {record.digest()[:12]}. A budget or a q edited after "
                "the campaign ran is the failure this project exists to prevent."
            )
        return record, stored


@dataclass
class CampaignResult:
    record: CampaignRecord
    record_digest: str
    rows: list[dict] = field(default_factory=list)
    wall_clock_s: float = 0.0
    tampered: bool = False

    @property
    def trials(self) -> list[dict]:
        return [r for r in self.rows if r.get("outcome") in TRIAL_OUTCOMES]

    @property
    def harness_failures(self) -> list[dict]:
        return [r for r in self.rows if r.get("outcome") == "harness_failed"]


def run_trial(source: str, *, profile: str, ledger: Path, campaign_id: str,
              index: int) -> dict:
    """One proposal, one isolated run, one ledger row. Returns the row.

    The strategy file is written, `run.py` is invoked exactly as a human would
    invoke it, and the row is read back. Nothing about the trial is an argument:
    dataset, split, seed and fill model all come from the committed profile.
    """
    strategy_path = REPO_ROOT / "src" / "strategies" / "current.py"
    original = strategy_path.read_text()
    before = _line_count(ledger)
    strategy_path.write_text(source)
    try:
        subprocess.run(
            [sys.executable, "run.py", "--human", "--quiet", "--purpose", "trial",
             "--profile", profile, "--ledger", str(ledger)],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
    finally:
        strategy_path.write_text(original)

    rows = _read_rows(ledger)
    if len(rows) <= before:
        # Every invocation appends exactly one row, so this cannot happen without
        # something being wrong with the harness rather than the candidate.
        return {"outcome": "harness_failed", "campaign_id": campaign_id,
                "campaign_index": index, "score": None,
                "error": "run.py produced no ledger row"}
    row = rows[-1]
    row["campaign_id"] = campaign_id
    row["campaign_index"] = index
    return row


def run_campaign(agent, record: CampaignRecord, *, ledger: Path,
                 record_path: Path, progress: bool = True) -> CampaignResult:
    """Run the pre-registered budget. No early stop on any observed result."""
    digest = record.write(record_path)
    t0 = time.monotonic()
    rows: list[dict] = []
    history: list[dict] = []
    for i in range(record.budget):
        source = agent.propose(history)
        row = run_trial(source, profile=record.profile, ledger=ledger,
                        campaign_id=record.campaign_id, index=i)
        rows.append(row)
        # What the agent is shown is a Task 31 decision. The stub ignores it, and
        # the loop passes the full row so that decision is made by whoever makes
        # it rather than by the shape of this list.
        history.append(row)
        if progress and (i + 1) % 25 == 0:
            done = sum(1 for r in rows if r.get("outcome") == "completed")
            print(f"  {i + 1}/{record.budget} trials, {done} completed, "
                  f"{time.monotonic() - t0:.0f}s", flush=True)

    tampered = False
    try:
        _, reread = CampaignRecord.read(record_path)
        tampered = reread != digest
    except CampaignTampered:
        tampered = True
    return CampaignResult(record=record, record_digest=digest, rows=rows,
                          wall_clock_s=time.monotonic() - t0, tampered=tampered)


def summarise(result: CampaignResult, *, dataset: str | None = None,
              null_samples: list | None = None, q: float | None = None,
              null_routes: list | None = None) -> dict:
    """The correction over the campaign, and the uncorrected view beside it.

    **`max_of_n` over per-candidate nulls is the gate; BH and Holm are
    diagnostics.** Task 32 demoted the tabulated FAR column to screening: it is
    one strategy's null, and `N <= q(R+1)` means no affordable resolution lets a
    per-trial p-value clear its BH bar at campaign scale. Pass `null_samples` --
    one null score sample per trial, in trial order -- to get the gate. Without
    it only the diagnostics are reported, and the summary says so rather than
    letting BH stand in.

    `null_routes` is the hybrid's per-trial routing, and the **rotation
    share it implies is a leading indicator rather than an accounting
    line**. A candidate that exploits an edge trades differently from an
    edgeless one, so it lands in a thin profile bucket and takes the
    expensive path: Task 33 measured 23.5% with no signal planted and 55.2%
    at strength 0.50. A campaign whose rotation share is climbing is
    producing something atypical, and its bill is going up. **A campaign's
    cost is therefore not known in advance**, and the null budget grows
    precisely when the loop starts working.
    """
    from ..scoring.calibration import null as nullmod
    from ..scoring.correction import correct, deflation_view, max_of_n, resolution_cap

    trials = result.trials
    fars, labels, scores, n_sessions = [], [], [], []
    for row in trials:
        score = row.get("score")
        label = f"{row.get('campaign_index')}:{(row.get('strategy_digest') or '')[:8]}"
        labels.append(label)
        if not score or score.get("rejected") or score.get("primary") is None:
            # A crashed or rejected trial enters at the worst possible p-value
            # rather than being dropped. It was run and looked at.
            fars.append(1.0)
            continue
        tup = _score_tuple(score)
        charge = nullmod.far_for_score(tup, dataset=dataset)
        fars.append(charge["far"])
        scores.append(score["primary"])
        n_sessions.append(score["n_sessions"])

    q = result.record.q if q is None else q
    corrected = correct(fars, q=q, labels=labels)
    out = {
        "campaign_id": result.record.campaign_id,
        "record_digest": result.record_digest,
        "record_tampered": result.tampered,
        "budget": result.record.budget,
        "q": result.record.q,
        "n_trials": len(trials),
        "harness_failures": len(result.harness_failures),
        "outcomes": _counts(r.get("outcome") for r in result.rows),
        "per_trial_accepts_at_0.001": sum(1 for f in fars if f <= 0.001),
        "diagnostics_only": {
            "note": ("BH and Holm over the tabulated FAR column are DIAGNOSTICS. "
                     "The column is one strategy's null (Task 30 measured 10-17x "
                     "its stated rate for candidates that trade differently), and "
                     "N <= q(R+1) caps how large a campaign a per-trial p-value "
                     "can survive at all."),
            "correction": corrected.as_dict(),
        },
        "wall_clock_s": round(result.wall_clock_s, 1),
    }
    if null_routes:
        rotation = sum(1 for r in null_routes
                       if r and r.get("route") == "rotation")
        out["null_routing"] = {
            "rotation_share": rotation / len(null_routes),
            "rotation": rotation,
            "bucket": sum(1 for r in null_routes
                          if r and r.get("route") == "bucket"),
            "unrouted": sum(1 for r in null_routes if not r),
            "note": ("a leading indicator: candidates exploiting an edge "
                     "have unusual profiles and take the expensive path. "
                     "23.5% with no signal, 55.2% at strength 0.50 "
                     "(Task 33). A rising share means the loop is producing "
                     "something atypical, and that the bill is going up."),
        }
    if null_samples:
        gate = max_of_n(null_samples, max(scores) if scores else float("-inf"), q=q)
        gate["is_the_gate"] = True
        out["gate"] = gate
    else:
        out["gate"] = {
            "is_the_gate": False,
            "note": ("no per-candidate nulls supplied, so this campaign has no "
                     "accept decision. BH over the tabulated column is not a "
                     "substitute -- see docs/design/metric.md."),
            "resolution": resolution_cap(2_000, q),
        }
    if scores:
        best = max(scores)
        out["uncorrected"] = {
            "best_score": best,
            "best_far": min(fars),
            "n_at_or_below_q": corrected.uncorrected_discoveries,
            "reading": (
                "what a report with no correction would say: the best of "
                f"{len(trials)} candidates scored {best:+.4f} at a nominal "
                f"false-acceptance rate of {min(fars):.4g}"
            ),
        }
        out["deflation"] = deflation_view(
            best, len(trials), max(set(n_sessions), key=n_sessions.count),
            dataset=dataset)
    return out


def _score_tuple(score: dict):
    from ..types import ScoreTuple

    return ScoreTuple(
        primary=score["primary"], sharpe=score["sharpe"], skew=score["skew"],
        kurtosis=score["kurtosis"], n_sessions=score["n_sessions"],
        net_pnl=score["net_pnl"], trade_count=score["trade_count"],
        rejected=score["rejected"], reject_reason=score["reject_reason"],
        metric=score["metric"], sortino=score.get("sortino", score["primary"]),
        downside_sessions=score.get("downside_sessions", 0),
    )


def _counts(values) -> dict:
    out: dict[str, int] = {}
    for v in values:
        out[str(v)] = out.get(str(v), 0) + 1
    return dict(sorted(out.items()))


def _line_count(path: Path) -> int:
    return len(_read_rows(path))


def _read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def new_campaign_id() -> str:
    return uuid.uuid4().hex[:12]
