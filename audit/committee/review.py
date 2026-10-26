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

# **The audit seats' own model, not the campaign's.** A different family from
# every proposer we run on purpose: the model that audits a strategy must not be
# the model that wrote it. mimo-v2.6-pro because it is the strongest model this
# project is authorised to use (docs/setup/MODEL-ACCESS.md) and four seats once
# per campaign is a cheap place to spend a slow model -- unlike the proposer,
# which calls twenty times a campaign.
AUDIT_MODEL = "mimo-v2.6-pro"
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
    usage: tuple = ()                  # one entry per call, for the record
    # The model that WROTE what these seats audited. Recorded so the question
    # "was this an independent check" is answerable from the artefact instead of
    # from reading the caller, which is how it went unnoticed for 17 campaigns.
    proposer_model: str = ""

    @property
    def vetoed_by(self) -> tuple[str, ...]:
        return tuple(a.axis for a in self.auditors if a.verdict == "VETO")

    @property
    def independent(self) -> bool | None:
        """Did a different model audit than wrote? `None` means unknowable.

        Tri-state on purpose. `False` is the defect; `None` is a record that
        does not say, and collapsing the two would let an unknown read as a
        pass the next time someone greps for it.
        """
        if not self.model or not self.proposer_model:
            return None
        return self.model != self.proposer_model

    @property
    def seats_failed(self) -> tuple[str, ...]:
        """Seats whose auditor never ran, by axis.

        **A seat that failed is recorded as a VETO with its failure as the
        reason**, which is the right refusal -- no verdict is not no veto -- and
        is invisible in every summary. 8 of 56 seats across the project are in
        this state and nothing said so, so a campaign judged by three auditors
        read exactly like one judged by four.
        """
        return tuple(a.axis for a in self.auditors
                     if any("did not run" in str(r) for r in (a.reasons or ())))

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
            # How many auditors actually ran. Stated rather than left to be
            # inferred from parsing reasons for "did not run".
            "seats_total": len(self.auditors),
            "seats_ran": len(self.auditors) - len(self.seats_failed),
            "seats_failed": list(self.seats_failed),
            "incomplete": bool(self.seats_failed),
            "incomplete_note": (
                "One or more auditors never ran, so this campaign was judged by "
                "fewer seats than the committee has. Their failure is recorded "
                "as a VETO because no verdict is not no veto, but the reading "
                "'the committee looked at this' is weaker than it appears."
                if self.seats_failed else ""),
            "usage": list(self.usage),
            "model": self.model,
            "proposer_model": self.proposer_model,
            "independent": self.independent,
            "independence_note": (
                "The auditors ran on the same model that wrote what they were "
                "auditing, which is not an independent check of it."
                if self.independent is False else
                "The proposer's model was not recorded, so whether this was an "
                "independent check cannot be determined from this file."
                if self.independent is None else ""),
            "model_served": self.model_served,
            "prompt_digests": self.prompt_digests,
        }


# --------------------------------------------------------------------------- #
# evidence slices -- each auditor sees ONLY its own
# --------------------------------------------------------------------------- #

def _breadth(buckets: dict | None) -> dict:
    """How many periods are positive, not how big the biggest one is.

    **Measured 2026-10-14, and it is the reason this function exists.** Across
    the 16 campaigns holding both a champion's diagnostics and a validation
    score, every concentration measure correlated POSITIVELY with out-of-sample
    performance -- top-5% share r = +0.217 (p = 0.42), best-session share
    r = +0.089 (p = 0.74), best-month share r = +0.412 (p = 0.11). More
    concentrated champions validated BETTER, and none of it was significant.

    The one quantity that cleared 0.05 was the **share of periods that are
    positive**: r = +0.584, p = 0.018. Breadth and concentration are not the
    same thing and they can disagree sharply -- nq-orderflow-02's best month was
    4.3% of its total, about as unconcentrated as a result gets, while only 3 of
    its 18 months were positive, and it validated at -0.4655.

    So the seat is handed both, and its prompt now leads with this one. n = 16
    and this is a correlation, not a mechanism: it is grounds for which number
    an auditor reads first, not for a threshold anybody computes.
    """
    if not isinstance(buckets, dict) or not buckets:
        return {"positive": None, "total": 0, "share_positive": None}
    vals = [v for v in buckets.values() if isinstance(v, (int, float))]
    if not vals:
        return {"positive": None, "total": 0, "share_positive": None}
    pos = sum(1 for v in vals if v > 0)
    return {"positive": pos, "total": len(vals),
            "share_positive": round(pos / len(vals), 4)}


def _validation_evidence(summary: dict) -> dict:
    """Section 5's three rules, or an explicit statement that they are missing.

    Never silently absent. "Out-of-sample was not checked" and "out-of-sample was
    checked and held up" are opposite readings of the same campaign, and an
    auditor handed neither will assume one of them.

    Rule (c), the train-to-validation change, is passed through **unjudged**. It
    is deliberately not a threshold (section 5): "within X%" needs an X, this
    project does not fit one, and a winner that does BETTER out of sample is not
    a failure. The auditor weighs it in context, which is the whole reason it is
    evidence rather than a gate.
    """
    v = summary.get("validation")
    if not v:
        return {"_validation": (
            "NOT RUN. The campaign winner was not evaluated on the validation "
            "split, so there is no out-of-sample evidence. Do not read this as "
            "the winner having failed out of sample, and do not read it as the "
            "winner having held up: it was not looked at.")}
    val, tr = v.get("validation") or {}, v.get("train") or {}
    return {"_validation_note": (
                "One look, on the held-out validation split, at the winner "
                "selected on train. Rule (c) below is the raw change and is NOT a "
                "pass/fail threshold -- weigh it; a winner that improves out of "
                "sample has not failed."),
            "train_primary": tr.get("primary"),
            "validation_primary": val.get("primary"),
            "validation_outcome": val.get("outcome"),
            "rule_a_sortino_positive_on_validation": v.get("rule_a_sortino_positive"),
            "rule_c_change_validation_minus_train": v.get("rule_c_decay"),
            "validation_score": val.get("score"),
            "validation_diagnostics": val.get("diagnostics")}


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
            **_validation_evidence(summary),
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
            **_validation_evidence(summary),
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
            **_validation_evidence(summary),
            # Breadth first: it is the measured predictor. See `_breadth`.
            "breadth_by_month": _breadth(diag.get("by_month")),
            "breadth_by_weekday": _breadth(diag.get("by_weekday")),
            "breadth_by_hour_utc": _breadth(diag.get("by_hour_utc")),
            "by_month": diag.get("by_month"),
            "by_hour_utc": diag.get("by_hour_utc"),
            # Both tables, because one of them describes the expiry calendar
            # and the other describes the strategy -- see `seasonality_caveat`.
            "by_hour_utc_entry": diag.get("by_hour_utc_entry"),
            "breadth_by_hour_utc_entry": _breadth(diag.get("by_hour_utc_entry")),
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

# **A gateway fault reaches us two different ways, and both must be retried.**
# A 502 or a timeout raises `LLMUnavailable`, which `_one` used to convert
# straight into a CANNOT_TELL verdict. A substitution raises `ModelSubstituted`,
# which propagates by design, because a committee that ran on a model its record
# does not name means nothing.
#
# Session 07 lost **6 of 12 seat-runs** to 502s: campaign 3 was judged by one
# auditor of four and its artefact still reads "the committee looked at this".
# Re-running those five seats by hand afterwards needed 11 attempts and hit
# timeouts, 502s AND a substitution -- a retry that handles only one of the two
# paths dies on the other, which is exactly what my first attempt at it did.
#
# Progressive, matching `research_session.STAGE_BACKOFF_S`: the substitution
# window was measured at several minutes on 2026-09-30 and outlasted 27 minutes
# on 2026-10-05, so short retries are not enough.
SEAT_BACKOFF_S = (30, 120, 300, 600, 600)
_TRANSIENT = ("502", "503", "504", "ReadTimeout", "ConnectTimeout", "Timeout",
              "ConnectionError", "RemoteDisconnected", "ServiceUnavailable")


def _is_transient(exc: Exception) -> bool:
    blob = f"{type(exc).__name__}: {exc}"
    return any(t in blob for t in _TRANSIENT)


def _one(axis: str, client: Client, summary: dict, source: str | None,
         hypothesis: str | None) -> AuditorVerdict:
    import time
    last = None
    for attempt in range(len(SEAT_BACKOFF_S) + 1):
        try:
            got = client.ask(_prompt(axis), _slice(axis, summary, source, hypothesis))
            break
        except ModelSubstituted as exc:
            last = exc                      # retried, never accepted
        except LLMUnavailable as exc:
            if not _is_transient(exc):
                return AuditorVerdict(
                    axis=axis, verdict="CANNOT_TELL",
                    reasons=(f"the auditor did not run: {exc}",), error=str(exc))
            last = exc
        if attempt == len(SEAT_BACKOFF_S):
            # Exhausted. Still CANNOT_TELL -- no verdict is not no veto -- but the
            # reason now says how hard we tried, so a reader can tell a flaky
            # endpoint from a seat nobody asked.
            return AuditorVerdict(
                axis=axis, verdict="CANNOT_TELL",
                reasons=(f"the auditor did not run after "
                         f"{len(SEAT_BACKOFF_S) + 1} attempts over "
                         f"{sum(SEAT_BACKOFF_S) // 60} minutes: {last}",),
                error=str(last))
        time.sleep(SEAT_BACKOFF_S[attempt])
    else:                                   # pragma: no cover - loop always breaks
        raise AssertionError("unreachable")
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


def review(summary: dict, *, source: str | None, model: str = AUDIT_MODEL,
           hypothesis: str | None = None, proposer_model: str = "",
           axes: tuple[str, ...] = AXES) -> CommitteeVerdict:
    """The four, in parallel, isolated. Returns a verdict that cannot approve.

    Preflight first: a committee that ran on a model its record does not name is
    a committee whose verdict means nothing, and `ModelSubstituted` propagates
    rather than degrading to CANNOT_TELL.

    **`model` defaults rather than being supplied by the campaign.** Until
    2026-10-04 `nq_campaign.py` passed the proposer's own model, so all 17
    recorded verdicts had a model auditing its own work -- `deepseek-v4-pro`
    reviewing `deepseek-v4-pro` on the last nine. `docs/setup/MODEL-ACCESS.md`
    called it a decision rather than a patch because fixing it changes what
    every past verdict means; Michael decided it on 2026-10-04. Pass
    `proposer_model` so the artefact can say whether the check was independent
    instead of leaving it to be inferred from the caller.
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
                            usage=tuple(client.calls),
                            model_served=served, prompt_digests=prompt_digests(),
                            proposer_model=proposer_model)
