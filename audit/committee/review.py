"""Four auditors, one campaign, in parallel and isolated. Any one can veto.

**Isolation is the mechanism, not a detail.** Each auditor gets its own call, its
own evidence slice, and never sees another's evidence or verdict. Show them each
other's findings and they anchor: the second agrees with the first, the fourth
rubber-stamps three PASSes, and "any one can veto" quietly becomes "the first one
decides". Four correlated opinions are one opinion with more tokens.

**Veto-only, and the vocabulary has no word for approval.** The same argument as
`tier3.Verdict`: an auditor that can only remove candidates is safe with a
completely uncharacterised error rate -- its mistakes cost power and can never
manufacture a discovery. Nobody has measured how often these four are wrong.

`docs/design/pre-registration.md` §6a records the MVP policy that a campaign no
auditor vetoes is approved, **and records its cost**: the error rate of "approve
when nobody objected" is unmeasured, and the FWER of 0.060 does not describe it.
That policy lives in the pre-registration and in the caller. It is deliberately
not expressible here -- this module returns vetoes and cannot-tells, and a caller
that wants to read silence as approval has to say so itself.

**What none of them can see:** how many campaigns came before. Two campaigns with
identical evidence, one the first look and one the twenty-seventh, are
indistinguishable to any reviewer, because the look count is not a property of the
campaign under review. The statistics prompt says so explicitly rather than
letting an auditor imagine otherwise.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

from .llm import Client, LLMUnavailable, ModelSubstituted, preflight

PROMPTS = pathlib.Path(__file__).parent / "prompts"
AXES = ("leakage", "statistics", "economics", "robustness")
VERDICTS = ("PASS", "VETO", "CANNOT_TELL")


@dataclass(frozen=True, slots=True)
class AuditorVerdict:
    axis: str
    verdict: str
    reasons: tuple[str, ...] = ()
    confidence_note: str = ""
    error: str | None = None

    def __post_init__(self) -> None:
        if self.verdict not in VERDICTS:
            raise ValueError(
                f"{self.axis}: {self.verdict!r} is not one of {VERDICTS}. There "
                "is deliberately no verdict meaning approval.")
        if self.verdict == "VETO" and not self.reasons:
            raise ValueError(f"{self.axis}: a veto states its reasons")


@dataclass(frozen=True, slots=True)
class CommitteeVerdict:
    verdict: str                       # PASS | VETO | CANNOT_TELL
    auditors: tuple[AuditorVerdict, ...]
    model: str = ""
    model_served: str = ""
    prompt_digests: dict = field(default_factory=dict)

    @property
    def vetoed_by(self) -> tuple[str, ...]:
        return tuple(a.axis for a in self.auditors if a.verdict == "VETO")

    def as_dict(self) -> dict:
        return {
            "schema": "committee-verdict/1",
            "verdict": self.verdict,
            "is_veto": self.verdict == "VETO",
            "vetoed_by": list(self.vetoed_by),
            "can_approve": False,
            "approval_note": (
                "The committee cannot approve. PASS means no auditor found "
                "anything to veto on its own axis with the evidence it was "
                "given. Reading that as approval is a caller's policy -- "
                "pre-registration.md 6a -- and its error rate is unmeasured."),
            "auditors": [asdict(a) for a in self.auditors],
            "model": self.model,
            "model_served": self.model_served,
            "prompt_digests": self.prompt_digests,
        }


# --------------------------------------------------------------------------- #
# evidence slices -- each auditor sees ONLY its own
# --------------------------------------------------------------------------- #

def _slice(axis: str, summary: dict, source: str | None,
           hypothesis: str | None = None) -> str:
    best = summary.get("best_candidate") or {}
    score = best.get("score") or {}
    diag = best.get("diagnostics") or {}
    n = summary.get("n") or {}

    if axis == "leakage":
        if not source:
            return "NO SOURCE AVAILABLE for the winning candidate."
        return f"The winning candidate's complete source:\n\n```python\n{source}\n```"

    if axis == "statistics":
        return json.dumps({
            "n": n,
            "gate": summary.get("gate"),
            "deflation_view": summary.get("deflation_view"),
            "diagnostics_only": summary.get("diagnostics_only"),
            "null_moments": summary.get("null_moments"),
            "null_source": summary.get("null_source"),
            "best_candidate_score": score,
            "error_control_note": summary.get("error_control_note"),
        }, indent=2, default=str)

    if axis == "economics":
        # `commission` and `gross_pnl` are in the diagnostics, and the rate is in
        # the campaign's agent_config -- not in `provenance`, which the first
        # draft guessed. The economics auditor's first live run said "commission
        # per contract was not provided", which is how the gap surfaced: an
        # auditor told what it could not see is more useful than one that quietly
        # reasons without it.
        campaign = summary.get("campaign") or {}
        cfg = campaign.get("agent_config") or {}
        # **Units, spelled out.** The first live run computed "6 fills at $0.10
        # each = $0.60" -- it read the commission RATE, which is in index points,
        # as dollars, and then compared it against a total that is also in index
        # points. Unlabelled numbers in an evidence slice are an invitation to
        # guess, and the guess was wrong in a way that looked authoritative.
        evidence = json.dumps({
            "_units": ("All P&L and commission figures are in INDEX POINTS, not "
                       "dollars. NQ is $20 per index point, so multiply by 20 for "
                       "dollars. commission_per_contract_per_side is also in index "
                       "points and is charged on EVERY fill; a round trip is two "
                       "fills."),
            "net_pnl_points": score.get("net_pnl"),
            "gross_pnl_points": diag.get("gross_pnl"),
            "commission_paid_points": diag.get("commission"),
            "commission_per_contract_per_side_points": cfg.get("commission_per_contract"),
            "usd_per_index_point": 20,
            "trade_count": score.get("trade_count"),
            "n_sessions": score.get("n_sessions"),
            "sessions_with_exposure": diag.get("sessions_with_exposure"),
            "primary": score.get("primary"),
            "metric": score.get("metric"),
            "avg_holding_period_ns": diag.get("avg_holding_period_ns"),
            "max_drawdown": diag.get("max_drawdown"),
            "fill_model_spread": diag.get("fill_model_spread"),
            "campaign": {k: campaign.get(k) for k in ("profile", "dataset", "split")},
        }, indent=2, default=str)
        return (
            "### The hypothesis this campaign was pre-registered under\n\n"
            + (hypothesis or "NOT SUPPLIED -- you cannot judge question 1.")
            + "\n\n### The winning strategy's source\n\n```python\n"
            + (source or "NO SOURCE AVAILABLE.")
            + "\n```\n\n### The economic evidence\n\n```json\n"
            + evidence + "\n```")

    if axis == "robustness":
        return json.dumps({
            "by_month": diag.get("by_month"),
            "by_hour_utc": diag.get("by_hour_utc"),
            "by_weekday": diag.get("by_weekday"),
            "best_session_share": diag.get("best_session_share"),
            "profit_concentration_top5pct": diag.get("profit_concentration_top5pct"),
            "sessions_with_exposure": diag.get("sessions_with_exposure"),
            "seasonality_caveat": diag.get("seasonality_caveat"),
            "downside_sessions": score.get("downside_sessions"),
            "n_sessions": score.get("n_sessions"),
            "trade_count": score.get("trade_count"),
            "outcomes": n.get("by_outcome"),
        }, indent=2, default=str)

    raise ValueError(f"unknown axis {axis!r}")


def _prompt(axis: str) -> str:
    return ((PROMPTS / "_shared.md").read_text() + "\n\n"
            + (PROMPTS / f"{axis}.md").read_text())


def prompt_digests() -> dict:
    """The prompts shaped the verdicts, so their versions go in the record."""
    out = {}
    for p in sorted(PROMPTS.glob("*.md")):
        out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    return out


# --------------------------------------------------------------------------- #

def _one(axis: str, client: Client, summary: dict, source: str | None,
         hypothesis: str | None) -> AuditorVerdict:
    try:
        got = client.ask(_prompt(axis), _slice(axis, summary, source, hypothesis))
    except ModelSubstituted:
        raise
    except LLMUnavailable as exc:
        return AuditorVerdict(axis=axis, verdict="CANNOT_TELL",
                              reasons=(f"the auditor did not run: {exc}",),
                              error=str(exc))
    verdict = str(got.get("verdict", "")).upper()
    if verdict not in VERDICTS:
        return AuditorVerdict(
            axis=axis, verdict="CANNOT_TELL",
            reasons=(f"returned {verdict!r}, which is not a verdict",),
            error="unparseable verdict")
    reasons = tuple(str(r) for r in (got.get("reasons") or []))
    if verdict == "VETO" and not reasons:
        reasons = ("vetoed without stating a reason",)
    return AuditorVerdict(axis=axis, verdict=verdict, reasons=reasons,
                          confidence_note=str(got.get("confidence_note", "")))


def review(summary: dict, *, source: str | None, model: str,
           hypothesis: str | None = None,
           axes: tuple[str, ...] = AXES) -> CommitteeVerdict:
    """The four, in parallel, isolated. Returns a verdict that cannot approve.

    Preflight first: a committee that ran on a model its record does not name is
    a committee whose verdict means nothing, and `ModelSubstituted` propagates
    rather than degrading to CANNOT_TELL.
    """
    served = preflight(model)
    client = Client(model=model)
    with ThreadPoolExecutor(max_workers=len(axes)) as pool:
        verdicts = tuple(pool.map(
            lambda a: _one(a, client, summary, source, hypothesis), axes))

    if any(v.verdict == "VETO" for v in verdicts):
        overall = "VETO"
    elif any(v.verdict == "CANNOT_TELL" for v in verdicts):
        overall = "CANNOT_TELL"
    else:
        overall = "PASS"
    return CommitteeVerdict(verdict=overall, auditors=verdicts, model=model,
                            model_served=served, prompt_digests=prompt_digests())
