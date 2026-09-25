"""The synthetic probe: run the proposal before the trial counts, tell the agent
three facts, and never a number that depends on market data.

**The problem it addresses.** The proposer gets one shot per trial and no
feedback until a 35-minute NQ run has been spent. `nq-ml-02` lost two of twenty
trials to edits that produced byte-identical fills to the champion, and
`nq-ml-01` lost thirteen to a crash class the static check has since closed.
Gowtam's `loop/` driver avoided this by letting its worker *execute* code; this
project will not hand the model a shell. So the harness executes on the model's
behalf, on synthetic data, and hands back the least it can.

**Why this is not the check Task 23 rejected.** `scripts/agent_check.py` records
the attack: a strategy that raises when its own P&L crosses a threshold turns a
pass/fail exit code into a score channel, and eighteen probes recover peak P&L
to four decimals with zero trials counted. That attack works because the probe
ran against *the data the trial is scored on*. This one runs against
`SyntheticDataset` -- a seeded noise generator with `signal_strength: 0`, built
before the first real adapter existed and calibrated from nothing -- so every
bit the agent can extract is a bit about synthetic noise. The Task 23 binary
search still works here; what it recovers is the strategy's P&L on a series
that has no relationship to NQ, which is worth exactly nothing to selection on
NQ. The invariant the static check states -- *no output is a function of NQ
prices* -- holds for this probe by construction of its dataset, not by
narrowing its channel.

**What crosses to the agent, and only this:**

- whether the run crashed, and the tail of the traceback;
- how many trades it made on the probe's sessions;
- whether its probe fill log is byte-identical to the champion's.

Never the probe's score, its P&L, or its per-session anything. `ProbeResult`
has no field for them, so a future edit that wanted to pass one would have to
add it here, in a file whose docstring says why not.

**What it changes about N: nothing.** The trial is still one scored run of the
registered profile, the crash-is-a-trial rule still applies to that run, and a
proposal that fails the probe after its repair budget *proceeds to the trial
anyway* -- the probe informs, it does not gate. It cannot shrink N: the budget is
fixed at registration and every slot is spent. What it does is convert slots
that would have been spent learning a synthetic-reproducible fact into scored
looks, which is what the budget was for. Every probe run is a `purpose: check`
row in the campaign's own `probe-ledger.jsonl`, so nothing ran without a trace.

Pre-registered as Amendment 7 (`docs/design/pre-registration.md`) and frozen
per campaign as `probe_before_trial`. Off unless a campaign registers it on.
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from .._roots import repo_root
from .campaign import declares_fit

REPO_ROOT = repo_root()

# Which committed profile a probe runs under. Chosen by the *proposal*, not by
# the campaign: a fitted campaign still receives unfitted proposals, and the
# engine refuses a strategy with no `fit()` under a profile that has a fit
# window (and vice versa). Both are synthetic, seeded, `signal_strength: 0`.
PROBE_PROFILES = {"plain": "agent", "fitted": "harness-fitted"}

# The last lines of a child traceback are the ones that name the fault. Bounded
# so a pathological traceback cannot become a prompt on its own.
TRACEBACK_TAIL_CHARS = 1_500


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Everything the agent may be told about a probe run. Nothing else."""

    ran: bool                 # False when the harness itself failed to run it
    crashed: bool
    profile: str
    trade_count: int | None
    fill_log_digest: str | None
    error: str                # the run's error string, "" when completed
    traceback_tail: str       # the last TRACEBACK_TAIL_CHARS of the child traceback
    wall_clock_s: float | None

    def as_dict(self) -> dict:
        return asdict(self)


def probe_profile_for(source: str) -> str:
    return PROBE_PROFILES["fitted" if declares_fit(source) else "plain"]


def probe_source(source: str, *, ledger: Path,
                 strategy_path: Path | None = None) -> ProbeResult:
    """Run `source` once under its synthetic probe profile; return the facts.

    Writes `src/strategies/current.py`, invokes `run.py` exactly as
    `run_trial` does, restores the file in a `finally`, and reads the row it
    appended. The row is the harness's account of the run; this function
    projects it onto `ProbeResult` and discards the rest -- including the score,
    which `run.py` computes for every row and which must not reach the agent.
    """
    strategy_path = strategy_path or REPO_ROOT / "src" / "strategies" / "current.py"
    profile = probe_profile_for(source)
    original = strategy_path.read_text()
    before = _line_count(ledger)
    strategy_path.write_text(source)
    try:
        subprocess.run(
            [sys.executable, "run.py", "--human", "--quiet", "--purpose", "check",
             "--profile", profile, "--ledger", str(ledger)],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
    finally:
        strategy_path.write_text(original)

    rows = _read_rows(ledger)
    if len(rows) <= before:
        return ProbeResult(ran=False, crashed=False, profile=profile,
                           trade_count=None, fill_log_digest=None,
                           error="run.py produced no ledger row",
                           traceback_tail="", wall_clock_s=None)
    return result_from_row(rows[-1], profile)


def result_from_row(row: dict, profile: str) -> ProbeResult:
    """The projection. Kept separate from the subprocess so it can be tested on
    a row without running one, and so the whitelist is one function long."""
    outcome = row.get("outcome")
    if outcome == "harness_failed" or outcome is None:
        return ProbeResult(ran=False, crashed=False, profile=profile,
                           trade_count=None, fill_log_digest=None,
                           error=str(row.get("error") or "harness failed"),
                           traceback_tail="", wall_clock_s=row.get("wall_clock_s"))
    score = row.get("score") if isinstance(row.get("score"), dict) else {}
    trades = score.get("trade_count")
    if trades is None:
        trades = row.get("fills_logged")
    tb = str(row.get("child_traceback") or "")
    return ProbeResult(
        ran=True,
        crashed=(outcome != "completed"),
        profile=profile,
        trade_count=None if trades is None else int(trades),
        fill_log_digest=row.get("fill_log_digest") or None,
        error="" if outcome == "completed" else str(row.get("error") or outcome),
        traceback_tail=tb[-TRACEBACK_TAIL_CHARS:],
        wall_clock_s=row.get("wall_clock_s"),
    )


def _line_count(path: Path) -> int:
    return len(path.read_text().splitlines()) if path.exists() else 0


def _read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
