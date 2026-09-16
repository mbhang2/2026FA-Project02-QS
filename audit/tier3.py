"""Tier 3: read a tier-2 campaign summary, return a verdict that cannot promote.

    python -m audit.tier3 runs/campaigns/task36/history

**The interface is a file in both directions.** Tier 2 writes
`tier2-summary.json`; this reads it and writes `tier3-verdict.json`. Nothing here
imports `src.core` and nothing there imports this, which is what lets either side
be replaced without touching the other -- the same property that let
`Gowtam/L1/loop/evaluate_step.py` become a second driver over `run.py`.

## Veto-only, and why it is structural rather than a policy

`Verdict` has **three members and there is no fourth**. There is no PROMOTE, no
ACCEPT, no score, no weight, no confidence that could be read as one: an approval
is not expressible in the type, so it cannot be expressed by a prompt that
drifted, a model that hallucinated, or a caller that felt strongly.

That is not fastidiousness about enums. An auditor that can only *remove*
candidates is safe with a completely uncharacterised error rate -- its mistakes
cost power and can never manufacture a discovery. An auditor that can promote has
to have its error rate measured before anyone may act on it, and nobody has
measured this one.

**CANNOT_TELL is a required member, not a convenience.** Task 35 found a
committee design whose ranking mode forbade "cannot tell", and Task 34's most
valuable output was declining to claim what the data could not support. A
CANNOT_TELL is **not a veto**: it escalates to a human and is recorded as having
done so.

## Tier ownership: this does not count trials and does not re-correct

Tier 2 owns the multiple-testing correction. `N` is read from the summary --
where it means what `runs/ledger.jsonl` means, every trial including the crashed
ones -- and is used to display and to sanity-check, never to compute a bar.
`len(glob("experiments/*.json"))` was the old denominator and is not used on this
path at all.

## DSR is a cross-check here, not a gate

`engine/stats.expected_max_sharpe` and the harness's simulated best-of-N answer
the same question two ways. Task 42 adjudicated them against a 400,000-draw
reference and the harness was closer at every N, with the closed form biased high
by +0.017 to +0.036 in Sharpe units. So this runs the closed form against the
summary's null moments and **reports agreement or disagreement**; it does not
override, and neither side is made more permissive to match the other.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

SCHEMA = "tier3-audit-verdict/1"

# N's provenance on this path. The constant `engine/audit.py` carries describes
# the ticket path, where N is a file count; here it comes from the ledger.
N_TRIALS_PROVENANCE_TIER2 = (
    "read from the tier-2 campaign summary; N as runs/ledger.jsonl defines it "
    "-- every trial including crashes, global across datasets, written as the "
    "trials happen. Tier 3 displays and sanity-checks it and never recomputes a "
    "bar from it."
)


class Verdict(Enum):
    """Three members. There is no fourth, and that is the guarantee."""

    PASS = "PASS"                  # nothing found to veto. NOT an approval.
    FAIL = "FAIL"                  # a veto, with reasons
    CANNOT_TELL = "CANNOT_TELL"    # escalates to a human; not a veto


# Asserted here as well as in the tests: a member added later is a member that
# changes what this tier can do, and it should be impossible to add one by
# accident.
assert len(Verdict) == 3, "tier 3 has exactly three verdicts"
assert not any(v.name in {"PROMOTE", "ACCEPT", "APPROVE"} for v in Verdict)


@dataclass(frozen=True, slots=True)
class AuditVerdict:
    """What tier 3 returns. Frozen, and the field list is the whole vocabulary.

    No `score`, no `confidence`, no `weight`, no `promote`. A field that ranks or
    scores a candidate is a field somebody will threshold, and a threshold on an
    auditor's output is an approval with extra steps.
    """

    verdict: Verdict
    reasons: tuple[str, ...] = ()
    checks: dict[str, Any] = field(default_factory=dict)
    escalate_to_human: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.verdict, Verdict):
            raise TypeError(
                f"verdict must be a Verdict, not {type(self.verdict).__name__}. "
                "Strings are how a fourth one gets in."
            )
        if self.verdict is Verdict.FAIL and not self.reasons:
            raise ValueError("a veto states its reasons")

    def as_dict(self) -> dict:
        return {
            "schema": SCHEMA,
            "verdict": self.verdict.value,
            "is_veto": self.verdict is Verdict.FAIL,
            "escalate_to_human": self.escalate_to_human,
            "reasons": list(self.reasons),
            "checks": self.checks,
            "can_promote": False,
            "promotion_note": (
                "Tier 3 cannot promote. The verdict type has three members and "
                "none of them means approval; PASS means nothing was found to "
                "veto. An auditor that only removes candidates is safe with an "
                "uncharacterised error rate, and this one's has not been "
                "characterised."
            ),
            "n_trials_provenance": N_TRIALS_PROVENANCE_TIER2,
        }


# --------------------------------------------------------------------------- #
# the checks
# --------------------------------------------------------------------------- #

def deflation_cross_check(summary: dict) -> dict:
    """daksh's closed-form E[max] against the harness's simulated one.

    Not a gate. Reports agreement or disagreement, as Task 41 did for the fill
    models and the metric sets.
    """
    from audit.engine.stats import expected_max_sharpe

    moments = summary.get("null_moments") or {}
    gate = summary.get("gate") or {}
    n = (summary.get("n") or {}).get("trials")
    if not moments.get("available") or gate.get("null_max_mean") is None or not n:
        return {"available": False,
                "why": "the summary carries no null moments or no simulated max"}

    sd, mean = moments["sd"], moments["mean"]
    closed = mean + sd * expected_max_sharpe(n_trials=int(n), sr_variance=1.0)
    simulated = float(gate["null_max_mean"])
    observed = float(gate.get("observed_best") or 0.0)
    return {
        "available": True,
        "n_trials": int(n),
        "pooled_null_mean": mean,
        "pooled_null_sd": sd,
        "e_max_closed_form_daksh": closed,
        "e_max_simulated_harness": simulated,
        "difference": closed - simulated,
        "observed_best": observed,
        "closed_form_bar_cleared": observed > closed,
        "simulated_bar_cleared": observed > simulated,
        "agree_on_the_margin": (observed > closed) == (observed > simulated),
        "gate_p": gate.get("p"),
        "gate_verdict": gate.get("verdict"),
        "neither_is_the_gate": (
            "Both numbers are E[max], the DSR-style margin. The gate is "
            "P(max >= observed) over the whole simulated distribution, which is "
            "a different and stricter question -- Task 36 found `deflation_view` "
            "clearing its bar in both arms while the gate rejected both."
        ),
        "note": (
            "The two routes assume different things about the null. The closed "
            "form assumes N draws from ONE null; `max_of_n` takes the maximum "
            "over N draws from N DIFFERENT nulls, because the conditional "
            "framing (Task 32) made the null per-candidate. Where the "
            "per-candidate nulls differ in location, the pooled mean sits below "
            "the candidates that actually drive the maximum, and the closed "
            "form's bar comes out too LOW -- the opposite of its direction on "
            "homogeneous input, which Task 42 measured as +0.017 to +0.036 high."
        ),
    }


def structural_checks(summary: dict) -> tuple[list[str], list[str]]:
    """Things that make a campaign unauditable. Returns (vetoes, unknowns)."""
    vetoes: list[str] = []
    unknowns: list[str] = []
    campaign = summary.get("campaign") or {}
    n = summary.get("n") or {}
    provenance = summary.get("provenance") or {}

    if campaign.get("record_tampered"):
        vetoes.append(
            "the pre-registered record does not hash to what it hashed to "
            "before the campaign ran; the budget or q may have moved")
    if campaign.get("record_digest") != campaign.get("record_digest_now"):
        vetoes.append("the campaign record's digest changed after the run")
    if n.get("unaccounted"):
        vetoes.append(
            f"{n['unaccounted']} ledger rows are neither a trial nor an "
            "admitted harness failure, so N is short by exactly those rows")
    if not provenance.get("code_fingerprint_unique_across_trials", True):
        vetoes.append("trials disagree about the code that produced them")
    if (summary.get("holdout") or {}).get("seen"):
        vetoes.append("the summary carries holdout data")

    isolation = (provenance.get("isolation") or {}).get("level")
    if isolation and isolation != "isolated":
        unknowns.append(
            f"isolation level is {isolation!r}: on this platform the strategy "
            "process cannot be given a memory ceiling, so a resource-exhaustion "
            "escape cannot be ruled out from the record alone")
    if not (summary.get("gate") or {}).get("is_the_gate"):
        unknowns.append("this campaign has no gate verdict to audit")
    best = summary.get("best_candidate") or {}
    if not best.get("source"):
        unknowns.append(
            "the best candidate's source is absent, so leakage review of the "
            "code itself was not possible")
    return vetoes, unknowns


def audit_campaign_summary(summary: dict) -> AuditVerdict:
    """The whole of tier 3's decision on one campaign summary."""
    vetoes, unknowns = structural_checks(summary)
    cross = deflation_cross_check(summary)
    checks = {"deflation_cross_check": cross, "unknowns": unknowns}

    if cross.get("available") and not cross["agree_on_the_margin"]:
        unknowns.append(
            "the two deflation routes disagree on the margin: closed form "
            f"{cross['e_max_closed_form_daksh']:+.4f} vs simulated "
            f"{cross['e_max_simulated_harness']:+.4f} against an observed "
            f"{cross['observed_best']:+.4f}")
    if (cross.get("available") and cross["closed_form_bar_cleared"]
            and (summary.get("gate") or {}).get("verdict") == "REJECT"):
        unknowns.append(
            "the deflated-Sharpe bar sits BELOW the observed best while tier 2 "
            "rejected: a deflation computed from one pooled null would have "
            "reported a discovery here. Tier 2's per-candidate nulls are the "
            "reason it did not, and this cross-check is not evidence against "
            "tier 2 -- it is evidence about the pooled-null assumption")

    if vetoes:
        return AuditVerdict(Verdict.FAIL, tuple(vetoes), checks)
    if unknowns:
        return AuditVerdict(Verdict.CANNOT_TELL, tuple(unknowns), checks,
                            escalate_to_human=True)
    return AuditVerdict(Verdict.PASS, (), checks)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("arm_dir", type=Path)
    a = ap.parse_args(argv)

    path = a.arm_dir / "tier2-summary.json"
    summary = json.loads(path.read_text())
    verdict = audit_campaign_summary(summary)

    record = verdict.as_dict()
    record["campaign_id"] = (summary.get("campaign") or {}).get("campaign_id")
    record["tier2_summary_digest"] = summary.get("digest")
    record["tier2_gate_verdict"] = (summary.get("gate") or {}).get("verdict")
    record["n_trials"] = (summary.get("n") or {}).get("trials")
    record["ledger"] = (summary.get("ledger") or {}).get("path")
    record["audited_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    record["digest"] = hashlib.sha256(
        json.dumps({k: v for k, v in record.items() if k != "audited_at"},
                   sort_keys=True, default=str).encode()).hexdigest()

    out = a.arm_dir / "tier3-verdict.json"
    out.write_text(json.dumps(record, indent=2, sort_keys=True, default=str) + "\n")
    print(f"campaign   {record['campaign_id']}")
    print(f"tier 2     {record['tier2_gate_verdict']}   N {record['n_trials']}")
    print(f"tier 3     {record['verdict']}"
          + ("   (escalated)" if record["escalate_to_human"] else ""))
    for r in record["reasons"]:
        print(f"  - {r}")
    c = record["checks"]["deflation_cross_check"]
    if c.get("available"):
        print(f"DSR check  E[max] closed form {c['e_max_closed_form_daksh']:+.4f}"
              f"   simulated {c['e_max_simulated_harness']:+.4f}"
              f"   diff {c['difference']:+.4f}")
        print(f"           observed {c['observed_best']:+.4f}  "
              f"clears closed-form bar: {c['closed_form_bar_cleared']}  "
              f"clears simulated bar: {c['simulated_bar_cleared']}  "
              f"agree: {c['agree_on_the_margin']}")
    print(f"wrote      {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
