"""What an outer loop may see about a campaign that already ran.

Lifted from Gowtam's `TrajectoryLeak` (`origin/Gowtam/HypInt`), which is the
best single idea in his branch and is sharper than what we had.

`src.core.loop.llm._history_block` calls itself a whitelist and is really
*selection*: it reaches into a 45-key ledger row and picks six fields. That
cannot leak -- you cannot leak what you never read -- but it is **silent when
the row shape changes**, and those rows gained diagnostic fields on
2026-10-28. His version refuses a row carrying anything it does not expect,
which turns a widening row into a failure instead of a surprise.

Two rules, and the second is his too:

* **fail closed.** An unknown key raises `TrajectoryLeak`. His comment makes
  the argument and it is the right one: "a blacklist over a growing row shape
  leaks the first field somebody adds."
* **direction, not magnitude.** `score_dir` is `up` / `down` / `flat`, never
  the number. A loop choosing what to research next does not need the score to
  do that, and a loop that can see the score can climb it. This is NOT how the
  strategy-writing proposer is treated -- it gets the magnitude, because it has
  to iterate on its own code -- and the asymmetry is deliberate.

Nothing here reads a p-value, an accept/reject verdict, N, a null statistic, a
validation number or a holdout anything. §4 permits campaign k+1 to depend on
**train** results, so that is the whole surface this exposes.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The only keys a trajectory row may carry. A whitelist, not a blacklist.
ALLOWED = frozenset({
    "campaign_id", "outcome", "trials", "scored", "sessions",
    "score_dir", "turnover_dir", "activity_dir", "bwis_band",
})

#: Named so a reader can see what was deliberately excluded rather than
#: guessing from the absence. Every one of these is either the gate's output or
#: a number a loop could hill-climb.
REFUSED_FOR_CAUSE = {
    "p": "the gate's output",
    "p_value": "the gate's output",
    "accepted": "the gate's verdict",
    "rejected": "the gate's verdict",
    "primary": "the score itself; score_dir carries the direction",
    "score": "the score itself; score_dir carries the direction",
    "significance": "a monotone function of the score",
    "n": "the trial count the correction uses",
    "q": "the gate's threshold",
    "validation": "section 4: k+1 may never depend on validation",
    "holdout": "section 8",
}


class TrajectoryLeak(RuntimeError):
    """A row carried a field an outer loop may never see."""


@dataclass(frozen=True, slots=True)
class Trajectory:
    campaign_id: str
    outcome: str
    trials: int | None = None
    scored: int | None = None
    sessions: int | None = None
    score_dir: str | None = None
    turnover_dir: str | None = None
    activity_dir: str | None = None
    bwis_band: str | None = None


def direction(before: float | None, after: float | None,
              *, flat_within: float = 0.0) -> str | None:
    """`up` / `down` / `flat` from two numbers, neither of which escapes.

    `flat_within` exists because "it moved by 1e-9" is not a direction, and a
    loop told it moved will act as though it did.
    """
    if before is None or after is None:
        return None
    delta = after - before
    if abs(delta) <= flat_within:
        return "flat"
    return "up" if delta > 0 else "down"


def to_trajectory(row: dict) -> Trajectory:
    """Whitelist a row into a `Trajectory`, refusing anything unexpected."""
    leaked = sorted(set(row) - ALLOWED)
    if leaked:
        why = ", ".join(
            f"{k} ({REFUSED_FOR_CAUSE[k]})" if k in REFUSED_FOR_CAUSE else k
            for k in leaked)
        raise TrajectoryLeak(
            f"a trajectory row carried fields an outer loop may not see: {why}. "
            f"Build the row explicitly from the whitelist rather than passing a "
            f"ledger row through -- the point of failing here is that a row "
            f"which grows a field does not silently start leaking it.")
    if not row.get("campaign_id"):
        raise TrajectoryLeak("a trajectory needs campaign_id to be readable")
    return Trajectory(
        campaign_id=str(row["campaign_id"]),
        outcome=str(row.get("outcome", "")),
        trials=row.get("trials"), scored=row.get("scored"),
        sessions=row.get("sessions"), score_dir=row.get("score_dir"),
        turnover_dir=row.get("turnover_dir"),
        activity_dir=row.get("activity_dir"),
        bwis_band=row.get("bwis_band"),
    )
