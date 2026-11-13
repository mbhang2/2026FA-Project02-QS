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

from .._roots import repo_root

# Was `Path(__file__).resolve().parents[3]`. The nine modules under
# src/core/ counted at three different depths, which is the evidence
# that a count describes this file rather than the repository.
REPO_ROOT = repo_root()

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
    # The campaign's question: the file, and what it hashed to when the record
    # was written. Both None for a campaign that has none -- and `digest()`
    # drops None keys, so adding these two fields does not move the digest of
    # any record committed before they existed. Editing the hypothesis file
    # mid-campaign changes `hypothesis_digest` and the record then fails its own
    # check, which is the same alarm an edited budget rings.
    hypothesis_path: str | None = None
    hypothesis_digest: str | None = None
    # Everything a non-deterministic agent needs pinned (Task 36): model id,
    # temperature and sampling parameters, whatever seed the API accepts, the
    # prompt template digest, what the agent is shown, and the invocation
    # bounds. `None` for a stub campaign, which is why `digest()` drops None
    # keys -- adding a field must not invalidate a record committed before it
    # existed, and a record whose digest moved for a reason unrelated to its
    # contents is a tamper alarm that cries wolf.
    agent_config: dict | None = None
    # **May the proposing agent write a strategy that fits?** `None` and `False`
    # both mean no; only an explicit `True` allows it. `None` rather than `False`
    # as the default is load-bearing: `digest()` drops None keys, so a record
    # committed before this field existed hashes to the same value it always did.
    # A `False` default would move every one of them and `CampaignRecord.read`
    # would raise `CampaignTampered` on records nobody touched.
    #
    # Frozen into the record before trial 0, like the budget and the q. The
    # question it settles is pre-registered, not decided while looking at
    # results: "we allowed ML once the unfitted trials looked poor" is a
    # different experiment from the one that was registered.
    allow_fitted_proposals: bool | None = None
    # **Amendment 12: how many trials share one history.** `None` and `1` both
    # mean the chain this project ran for its first eighteen campaigns, where
    # every proposal sees every trial before it. Above 1, trials are proposed in
    # generations of this width: everyone in a generation sees every trial
    # completed before the generation began, and nothing from its own.
    #
    # `None` rather than `1` as the default for the same reason as the field
    # above: `digest()` drops None keys, so a record committed before this
    # existed hashes to what it always did and `CampaignRecord.read` does not
    # raise `CampaignTampered` on records nobody touched.
    generation_width: int | None = None
    # **Where trial 0's starting point came from, if it was given one.** A
    # campaign may begin from a human-written champion -- the division Michael
    # set out: human writes the initial version, the agent edits per trial -- or
    # from nothing, in which case the agent writes from scratch. Both are valid
    # and the record has to say which happened, because a campaign that started
    # from a working fitted strategy and one that had to invent it are not the
    # same experiment. None for no seed; `digest()` drops None, so records that
    # predate this field keep their hash.
    seed_path: str | None = None
    seed_digest: str | None = None
    # Amendment 6's exposure floor, frozen at registration. None for records
    # that predate it; `digest()` drops None so their hashes do not move.
    min_exposed_fraction: float | None = None
    # Amendment 7: was each proposal run once on synthetic data before its
    # trial, with a bounded repair on a crash or a no-op edit? Registered, not
    # decided at trial time, because it changes how the budget is spent. None
    # for records that predate it; `digest()` drops None.
    probe_before_trial: bool | None = None
    # **Which multiplicity rule the gate uses, frozen before trial 0.**
    #
    # None == "independent", the rule every campaign before this used: each
    # trial's null resampled on its own, so the maximum assumes the trials are
    # independent. They are not -- an agent that sees history writes correlated
    # candidates -- and `MAX_OF_N_NOTE` has always said so. Measured FWER
    # 0.0326, CI [0.0299, 0.0354]: CONSERVATIVE, which costs power and never
    # buys a false accept.
    #
    #   "dedup"   trials sharing a blotter digest are ONE look. Identical
    #             blotter, identical test; counting it twice is arithmetic,
    #             not caution. Measured FWER 0.0486, CI [0.0452, 0.0519].
    #   "aligned" Westfall-Young: one randomisation per replicate, shared
    #             across trials, maximum taken down the column. Measured FWER
    #             0.0482, CI [0.0449, 0.0515], and EXACT by exchangeability
    #             rather than merely calibrated. Subsumes "dedup".
    #
    # All three measured by `scripts/correction_fwer.py` over 16,000 no-signal
    # campaigns, the same way Task 57 established the current rule.
    #
    # On the record because it decides the p-value: a rule chosen after seeing
    # a result is the failure this file exists to prevent. And because
    # "aligned" is only READABLE off nulls that were GENERATED with a shared
    # seed, which only the registering run can arrange -- `stage_nulls` seeds
    # per trial otherwise, precisely so they are independent.
    correction_rule: str | None = None
    # **The knowledge snapshot, by path and digest.** The compiled view the
    # proposer was shown, frozen at register like the hypothesis. `digest()`
    # drops None, so a record written before this field hashes exactly as it
    # did -- and a record that HAS one carries it into the hash, which is what
    # makes editing the snapshot after registration a tamper rather than a
    # silent change of what the agent was told.
    #
    # Appended last. Daksh's two kdemo campaigns were registered with these
    # fields by an implementation that never reached his commit; the tests and
    # the committed campaign.json files were the specification.
    knowledge_path: str | None = None
    knowledge_digest: str | None = None

    def digest(self) -> str:
        payload = json.dumps({k: v for k, v in asdict(self).items() if v is not None},
                             sort_keys=True, default=str)
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

    @property
    def unaccounted(self) -> list[dict]:
        """Rows that are neither a trial nor a harness failure.

        There should never be any, and for thirty-five tasks there were none --
        because the stub always emitted valid Python. Task 36 found the case
        that produces them: a strategy that failed to *load* raised past
        `run.py`'s handlers, `LedgerRun` wrote the row it promises to write from
        an empty record, and the row arrived here with no `outcome` at all.
        Both `trials` and `harness_failures` filter on a known value, so the row
        matched neither and **left N without leaving a trace**.

        `run.py` is fixed. This property exists anyway, because the fix is a
        correct attribution and this is the check that the attribution happened:
        a denominator that can shrink silently is the one thing this project
        cannot have, and "no row is unaccounted" is a cheaper thing to assert
        than "every outcome is right".
        """
        known = set(TRIAL_OUTCOMES) | {"harness_failed"}
        return [r for r in self.rows if r.get("outcome") not in known]


def declares_fit(source: str) -> bool:
    """Does this proposal define a `fit` method, i.e. is it a fitted strategy?

    By AST rather than by `"fit" in source`, which matches a comment, a docstring
    and the word "profit". A proposal is refused on this answer, so a wrong answer
    either blocks honest work or lets a fitted strategy through a campaign that
    did not register one.

    Syntactically invalid source is not a fitted strategy -- it is a broken one,
    and `run.py` gives a better error for that than this function could.
    """
    import ast

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    return any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name == "fit"
               for node in ast.walk(tree))


def run_trial(source: str, *, profile: str, ledger: Path, campaign_id: str,
              index: int, allow_fitted: bool | None = None,
              root: Path | None = None, purpose: str = "trial") -> dict:
    """One proposal, one isolated run, one ledger row. Returns the row.

    The strategy file is written, `run.py` is invoked exactly as a human would
    invoke it, and the row is read back. Nothing about the trial is an argument:
    dataset, split, seed and fill model all come from the committed profile.

    **`root` is which working copy to run in**, and defaults to this repository,
    which is the only value any campaign used before Amendment 12.
    `src/strategies/current.py` is one hardwired path, so two trials in one
    checkout overwrite each other mid-flight and the ledger would look clean and
    be wrong; `scripts/worker_tree.py` builds the isolated copies a generation
    runs in. Measured on real NQ data: a worker reproduces this repository's
    score bit-identically, which is the only reason running elsewhere is allowed
    to be invisible in the result.
    """
    root = root or REPO_ROOT
    if declares_fit(source) and not allow_fitted:
        # Refused before `current.py` is touched and before `run.py` is invoked,
        # so the proposal never reaches the data. It is NOT a ledger row and does
        # not count toward N: N is looks taken at the data, and this took none.
        # It does spend one of the campaign's budget slots, which is visible in
        # the returned history.
        return {"outcome": "refused_fitted", "campaign_id": campaign_id,
                "campaign_index": index, "score": None,
                "error": "the proposal defines fit(), and this campaign did not "
                         "register allow_fitted_proposals: true. A fitted "
                         "strategy chooses its parameters from data, which is a "
                         "different experiment from the one registered."}

    strategy_path = root / "src" / "strategies" / "current.py"
    original = strategy_path.read_text()
    before = _line_count(ledger)
    strategy_path.write_text(source)
    try:
        subprocess.run(
            # **`purpose` is an argument only so a rehearsal can say `check`
            # (Amendment 13).** It defaults to `trial` and no agent reaches it:
            # the campaign driver passes it, and the driver takes it from the
            # rehearsal flag, not from the record and never from a proposal.
            [sys.executable, "run.py", "--human", "--quiet", "--purpose", purpose,
             "--profile", profile, "--ledger", str(ledger)],
            capture_output=True, text=True, cwd=str(root),
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
                        campaign_id=record.campaign_id, index=i,
                        allow_fitted=record.allow_fitted_proposals)
        # What it cost to get that proposal. The stub has no log and no cost;
        # an LLM agent has tokens, latency, repair attempts and a bill, and an
        # overnight campaign's bill should be readable off the record rather
        # than off an invoice. Attached to the row rather than to the summary
        # because it is a property of the trial.
        drain = getattr(agent, "drain_log", None)
        if drain is not None:
            row["agent_call"] = drain()
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
              null_routes: list | None = None,
              exclude: set | None = None) -> dict:
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

    `exclude` is a set of `campaign_index` values whose scores the gate's
    OBSERVED best is not taken over -- off-question trials under a registered
    `fidelity_policy: enforce` (`src/core/loop/fidelity.py`). N, the FAR column
    and the null samples are untouched: every excluded trial still counts and
    still contributes its null, so the bar is the best of N nulls while the
    observed side can only shrink. Conservative by construction.
    """
    from ..scoring.calibration import null as nullmod
    from ..scoring.correction import correct, deflation_view, max_of_n, resolution_cap

    trials = result.trials
    fars, labels, scores, n_sessions = [], [], [], []
    gate_scores = []
    exclude = exclude or set()
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
        if row.get("campaign_index") not in exclude:
            gate_scores.append(score["primary"])

    q = result.record.q if q is None else q
    corrected = correct(fars, q=q, labels=labels)
    if result.unaccounted:
        # Loud rather than a field nobody reads. Every row is a trial or an
        # admitted harness failure; a third category means N is wrong and every
        # corrected p-value below it is computed against the wrong denominator.
        raise RuntimeError(
            f"{len(result.unaccounted)} of {len(result.rows)} rows are neither a "
            f"trial nor a harness failure -- outcomes "
            f"{sorted({str(r.get('outcome')) for r in result.unaccounted})}. "
            "N would be short by exactly those rows. Fix the attribution; do not "
            "summarise around it."
        )
    out = {
        "campaign_id": result.record.campaign_id,
        "record_digest": result.record_digest,
        "record_tampered": result.tampered,
        "budget": result.record.budget,
        "q": result.record.q,
        "n_trials": len(trials),
        "harness_failures": len(result.harness_failures),
        "unaccounted": len(result.unaccounted),
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
        observed_best = max(gate_scores) if gate_scores else float("-inf")
        # The rule comes from the REGISTERED record, never from an argument.
        rule = (result.record.correction_rule or "independent")
        kw: dict = {}
        if rule == "dedup":
            # One look per distinct blotter. A row with no digest cannot be
            # matched to another, so it keeps its own identity rather than
            # being collapsed into a bucket of unknowns.
            kw["identities"] = [r.get("blotter_digest") or f"row-{i}"
                                for i, r in enumerate(result.rows)]
        elif rule == "aligned":
            kw["aligned"] = True
        elif rule != "independent":
            raise ValueError(
                f"unknown correction_rule {rule!r} in the registered record; "
                "expected 'independent', 'dedup' or 'aligned'")
        gate = max_of_n(null_samples, observed_best, q=q, **kw)
        gate["correction_rule"] = rule
        gate["is_the_gate"] = True
        # **The floor, and it is load-bearing rather than decorative.** `reject`
        # means "reject the null": the best of N beat what N nulls would produce.
        # Under the sign-flip null that is a statement about DIRECTIONAL SKILL and
        # not about profit, because holding the trade count fixed makes a
        # high-turnover candidate's null commission-dominated. Measured while
        # building `round_turn.py`: a strategy losing 3,275 points scored p =
        # 0.0005 against its own null. It beat coin flips and still lost money.
        #
        # So the campaign decision is the conjunction, and `accept` is the field
        # a caller should read. `reject` keeps its narrow meaning so nothing that
        # already reads it changes.
        gate["observed_best"] = observed_best
        gate["observed_best_positive"] = bool(observed_best > 0)
        gate["accept"] = bool(gate["reject"] and observed_best > 0)
        gate["excluded_off_question"] = sorted(exclude)
        gate["floor_note"] = (
            "accept = reject AND observed_best > 0. The second term is not a "
            "formality: under a sign-flip null a losing high-turnover candidate "
            "can clear the first one decisively.")
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
