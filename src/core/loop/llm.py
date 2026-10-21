"""The proposing agent, with a model behind it.

Everything here runs in the **loop driver**, which has network. The strategy
process does not, and nothing in this module is reachable from it: the driver
calls the API, writes the file, and then starts a sandboxed child that has never
heard of any of this. That ordering is the whole reason the API call is safe --
a candidate that could fetch the evaluation dates' real prices would produce a
false discovery with a genuine blotter, a stable dataset digest, a clean
fingerprint and a replay that reproduces.

**Four things are pre-registered rather than decided at the call site**, and all
four are in the campaign record:

*What the agent sees.* The API documents, the current strategy, and its own
prior trials with their scores. **Never the gate** -- no threshold, no p-value,
no false-acceptance rate, no accept/reject verdict, no N. `_history_block`
whitelists fields rather than dropping known-bad ones, because a blacklist over
a growing row shape leaks the first field somebody adds.

*Retries.* There are none. A call that fails, returns nothing, or returns
something unparseable is a **blocked proposal**, and a blocked proposal is a
crashed trial with a row that counts toward N. Silence is a failed dispatch, not
an invitation to try again: an agent that may retry until it gets a clean answer
has performed trials the ledger never saw, which is the cleanest attack there is
on honest N.

*Repair.* A **static** check failure is the one exception, and it is safe for a
reason Task 23 established and demonstrated: `agent_check` executes nothing, so
its pass/fail cannot be a function of market data. Repairing a syntax error
therefore leaks no selection signal. It is still bounded -- `max_repair_attempts`
per proposal, counted separately from N, and exhausting the bound is a blocked
proposal like any other.

*Bounds.* A per-call HTTP timeout and a per-proposal wall clock. Exceeding
either is blocked, never a hang.

**A campaign is not reproducible and this module does not pretend otherwise.**
Each *trial* is, from its stored source. The campaign is not, because the model
is not: `temperature`, the model id, whatever seed the endpoint honours and the
prompt template digest are recorded so the conditions are known, which is a
different and weaker claim than reproducibility. Saying so is the point.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Sequence

from .hypothesis import Hypothesis, block as hypothesis_block

if TYPE_CHECKING:  # pragma: no cover -- a type, never an import at run time
    from .probe import ProbeResult

from .._roots import repo_root

# Was `Path(__file__).resolve().parents[3]`. The nine modules under
# src/core/ counted at three different depths, which is the evidence
# that a count describes this file rather than the repository.
REPO_ROOT = repo_root()
TEMPLATE_PATH = REPO_ROOT / "config" / "prompts" / "strategy-agent.md"

# What the agent reads. Two are generated and one is hand-written; all three are
# the same documents a human contributor is pointed at, which is deliberate --
# an agent given a private, friendlier interface is not being measured on the
# interface the project actually ships.
DOC_PATHS = {
    "api": REPO_ROOT / "docs" / "agent" / "strategy-api.md",
    "context_api": REPO_ROOT / "docs" / "design" / "context-api.md",
    "data": REPO_ROOT / "docs" / "agent" / "data.md",
}

FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)

BLOCKED_TEMPLATE = '''"""This proposal was blocked. It is a trial and it counts toward N.

reason:  {reason}
detail:  {detail}

No strategy was produced, so there is nothing to run. The file raises at module
level on purpose: the run then fails to load, `run.py` records `outcome:
crashed` with this message, and the trial keeps its row in the ledger. The
alternative -- returning nothing and skipping the trial -- would shrink N by
exactly the proposals that failed, which is the cheapest way to cheat a
multiple-testing correction and the one nobody would look for.
"""

raise RuntimeError("agent_blocked: {reason}")
'''


def template_digest(path: Path = TEMPLATE_PATH) -> str:
    """The prompt is an artifact. This is its version."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


# A transport failure is retried this many times before the proposal is blocked.
#
# **Raised 2026-10-14 to match the committee's, because 4 minutes was not
# enough.** The first version of this -- 3 attempts over (15, 60, 180), about
# four minutes -- was added on 2026-10-01 after 24 trials had been lost to
# gateway `ReadTimeout`. Session 07 then lost **71 more trials to
# `agent_blocked: call_failed` with this retry already in place**: 59 in
# nq-s07-02 and 12 in nq-s07-03, 5.9% of a 1,200-trial budget. The same session
# lost 6 of 12 committee seats to the same gateway, and `review.py` answered
# that with `SEAT_BACKOFF_S = (30, 120, 300, 600, 600)` -- about 27 minutes.
# There was no reason for the proposer to give up seven times sooner than the
# auditor; it was simply written first and never revisited.
#
# The cost of waiting is wall clock. The cost of not waiting is a trial that
# **still counts toward N** -- `global_n` counts crashed trials, deliberately,
# because the look happened -- so every abandoned call inflates `max_of_n`'s
# multiplicity correction while contributing no candidate to it. Waiting is
# cheaper than that.
#
# Only 429 and 5xx reach here (see `_one_attempt`), so an exhausted budget
# (HTTP 422) still fails immediately instead of sleeping 27 minutes to no end.
# **Every call opts out of the gateway's response cache.**
#
# Measured 2026-10-20, after the gateway came back: four identical requests to
# `claude-haiku-4-5` at `temperature=0.7` returned ONE distinct answer -- the
# first in 2.04 s and the next three in 0.18-0.25 s. With `cache: no-cache`,
# four of four were distinct at ~0.97 s each. A 381-token completion in 0.12 s
# is ~3,200 tok/s and is not a generation.
#
# Why this is not a performance question:
#
# 1. **The search stops searching.** The proposer runs at `temperature=0.7`
#    precisely so that re-asking explores. A cache keyed on the request returns
#    the same proposal however high the temperature, so the W agents of a
#    generation -- which share a `shown` snapshot, and whose prompts therefore
#    coincide before any history accumulates -- would spend W looks on one
#    candidate. `global_n()` counts all W.
# 2. **Retrying a failure re-serves the failure.** A repair call whose prompt
#    repeats returns the identical broken code, so the retry cannot succeed and
#    the trial is lost deterministically rather than occasionally.
# 3. **It would silence the substitution guard.** `preflight` exists to read the
#    `model` field; served from cache it reports whichever model answered
#    earlier, which is exactly the failure Task 37 cost 35 trials to learn.
# 4. **Task 37 would stop being a control.** Its validity rests on generating
#    under Task 36's conditions, and Task 36 produced 200 distinct strategy
#    digests from 200 trials, most-common x1. A cached run cannot match that.
#
# Sent as a body parameter, not the `x-litellm-no-cache` header: the header was
# measured at 0.18 s (still cached) and the parameter at 0.68 s on the same
# prompt. Assumes a LiteLLM-compatible endpoint, which is what `base_url` has
# always meant here.
NO_CACHE = {"no-cache": True}

# The smallest `max_tokens` a preflight may send. A reasoning model spends its
# budget before emitting a visible token, and the gateway reports the empty
# output as `ContentPolicyViolationError: Response output was blocked`, which
# reads like a refusal and is a truncation. Measured 2026-09-25 on
# `gemini-3.1-pro-preview`: 4 of 4 failures at 16, HTTP 200 with an honest
# `model` field at 64. Named so the test asserts the floor rather than scraping
# the literal out of the call.
PREFLIGHT_FLOOR_TOKENS = 512

MAX_TRANSPORT_RETRIES = 5
TRANSPORT_BACKOFF_S = (30, 120, 300, 600, 600)


class LLMTransportError(RuntimeError):
    """The endpoint never answered, so there is no proposal to judge.

    **The distinction this class exists to draw.** A failed call and a bad
    proposal are different events, and conflating them cost this project 24
    trials. A model that returns something we dislike must NEVER be re-asked --
    that is a re-roll, and `max_of_n` is corrected over the proposals we made,
    not the ones we kept. But a `ReadTimeout` produced nothing at all: retrying
    it does not re-roll a proposal, it obtains the one that was always going to
    happen, and N is unchanged either way.

    Raised only for transport and for HTTP statuses that mean "ask again"
    (429, 5xx). Never for a parsed reply, never for a substituted model.
    """


class LLMError(RuntimeError):
    """The call did not produce a usable answer. Never retried."""


class ModelSubstituted(RuntimeError):
    """The endpoint answered with a different model than the one asked for.

    **A harness failure, never a candidate failure.** It is raised rather than
    logged because a campaign whose record says one model and whose trials ran
    on another is a campaign whose record is false, and every comparison drawn
    from it is wrong in a way no reader could detect.

    Task 37 found this live: `claude-haiku-4-5` started returning HTTP 503 and
    the gateway answered 200 with `"model": "gemini-3.1-pro-preview"`. The run
    in progress had to be discarded, and *how much* of it was affected could not
    be established -- because nothing recorded the model that actually served
    each call. That is the gap this class and `Call.model_served` close.
    """


@dataclass(frozen=True, slots=True)
class Call:
    """One API call, for the bill and for the record."""

    kind: str                 # "propose" | "repair" | "probe" | "probe_repair"
    ok: bool
    latency_s: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str = ""
    # What the endpoint says it answered with. `config()["model"]` is what was
    # asked for, and a gateway is free to serve something else.
    model_served: str = ""
    # **Why the reply was the length it was.** nq-ml-05 lost 4 of its first 10
    # trials to "no python block in the reply" and the log could not say why,
    # because neither of these was recorded. They are: gemini-3.1-pro-preview
    # spends ~12,000 tokens reasoning before its first visible token, so a
    # 16,384 budget left ~1,400 of headroom and a call whose thinking ran 10%
    # long truncated before the code fence closed. `audit/committee/llm.py`
    # has recorded both since the same defect silenced three of nq-ml-02's
    # four seats; the proposer did not, and so the third instance of one bug
    # had to be diagnosed by reproducing a call by hand.
    finish_reason: str = ""
    reasoning_tokens: int = 0

    def as_dict(self) -> dict:
        return {"kind": self.kind, "ok": self.ok,
                "latency_s": round(self.latency_s, 2),
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "model_served": self.model_served,
                "finish_reason": self.finish_reason,
                "reasoning_tokens": self.reasoning_tokens,
                "error": self.error}


@dataclass
class LLMAgent:
    """`propose(history) -> str`. The same one method the stub implements.

    `show_history=False` is the control arm of Task 36's dry run: identical in
    every other respect, and shown nothing about its own past. If the two
    trajectories do not differ, the loop is random search with a language model
    attached, and that is worth knowing before anything is built on top of it.
    """

    model: str
    temperature: float = 0.7
    max_tokens: int = 4096
    show_history: bool = True
    max_repair_attempts: int = 2
    request_timeout_s: float = 600.0
    proposal_wall_clock_s: float = 600.0
    history_full_sources: int = 2
    seed: int | None = None
    base_url: str = ""
    template_path: Path = TEMPLATE_PATH
    # The run conditions, as prose. The shipped documents describe the NQ
    # adapter and a campaign may not be on it; a candidate written against the
    # wrong instrument is a measurement of the prompt, not of the agent. Facts
    # only -- dataset, split name, sizes, budgets. Never whether there is an
    # edge to find, and never anything the correction computes.
    run_facts: str = ""
    # The campaign's question, or None. Linted and digested by
    # `src.core.loop.hypothesis.load` before it gets here -- this field takes a
    # `Hypothesis`, never a path and never raw text, so there is no route by
    # which unlinted prose reaches a prompt.
    hypothesis: "Hypothesis | None" = None
    # **What the agent is shown as "the current strategy".** `None` means the
    # committed baseline, which is what every campaign before Task 56 did: the
    # file never advanced, so the agent re-derived from the same starting point
    # every trial and its own best work reached it only as text in the history.
    #
    # Set this to a path and the loop gets Karpathy's semantics -- the champion
    # IS the base for the next edit. It is deliberately not
    # `src/strategies/current.py`: that file is an `ARTIFACT_GLOBS` entry, so
    # leaving a candidate in it would change the artifact fingerprint of every
    # subsequent run. The champion is campaign state and lives beside the ledger.
    champion: Path | None = None
    # **Amendment 7: run the proposal on synthetic data before the trial.**
    # `probe_before_trial` is the registered decision and goes in the record;
    # `probe` is the callable the campaign supplies (`probe.probe_source` bound
    # to the campaign's probe ledger). Both must be set for a probe to run, so
    # a record that says `false` cannot be probing and an agent built without
    # a ledger to write to cannot either. See `src/core/loop/probe.py` for what
    # crosses back and why it is not the check Task 23 rejected.
    probe_before_trial: bool = False
    probe: "Callable[[str], ProbeResult] | None" = None
    max_probe_repairs: int = 2
    # Everything below is state, not configuration.
    calls: list[Call] = field(default_factory=list, init=False, repr=False)
    _pending: list[Call] = field(default_factory=list, init=False, repr=False)
    _repairs: int = field(default=0, init=False, repr=False)
    _n: int = field(default=0, init=False, repr=False)
    # The reference file's own probe result, keyed by a digest of its text, so
    # "identical to the current strategy" costs one probe per champion rather
    # than one per trial.
    _reference_probe: dict = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.base_url = self.base_url or os.environ.get("LITELLM_BASE_URL", "")
        if not self.base_url:
            raise LLMError("LITELLM_BASE_URL is not set; the driver has no endpoint")
        if not os.environ.get("LITELLM_API_KEY"):
            raise LLMError("LITELLM_API_KEY is not set")
        self._template = self.template_path.read_text()
        self._docs = {k: v.read_text() for k, v in DOC_PATHS.items()}

    # -- the pre-registration, as data ------------------------------------- #
    def config(self) -> dict:
        """Exactly what goes into the campaign record. No secrets, by field."""
        return {
            "implementation": "src.core.loop.llm.LLMAgent",
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "seed": self.seed,
            "endpoint": self.base_url,
            "prompt_template": str(self.template_path.relative_to(REPO_ROOT)),
            "prompt_template_digest": template_digest(self.template_path),
            "documents_shown": sorted(
                str(p.relative_to(REPO_ROOT)) for p in DOC_PATHS.values()),
            "run_facts": self.run_facts,
            # The question, by path and digest rather than by value. The text
            # is in a committed file whose commit date is the pre-registration;
            # copying it into the record would make the record the second place
            # it lives, and two places is how they come to disagree.
            "hypothesis": (
                None if self.hypothesis is None else {
                    "path": str(self.hypothesis.path.relative_to(REPO_ROOT)),
                    "digest": self.hypothesis.digest,
                }),
            "champion_carry_forward": self.champion is not None,
            "show_history": self.show_history,
            "history_full_sources": self.history_full_sources,
            "shows_the_gate": False,
            "retries_allowed": False,
            "max_repair_attempts": self.max_repair_attempts,
            # Amendment 7. Recorded even when off, so a record that predates the
            # field and one that declined it read differently.
            "probe_before_trial": bool(self.probe_before_trial),
            "max_probe_repairs": self.max_probe_repairs,
            "request_timeout_s": self.request_timeout_s,
            "proposal_wall_clock_s": self.proposal_wall_clock_s,
        }

    # -- what the campaign reads back -------------------------------------- #
    def drain_log(self) -> dict:
        """The last proposal's calls, and what they cost. Called once per trial."""
        calls, self._pending = self._pending, []
        return {
            "calls": [c.as_dict() for c in calls],
            "n_calls": len(calls),
            "repair_attempts": sum(1 for c in calls if c.kind == "repair"),
            "probe_runs": sum(1 for c in calls if c.kind == "probe"),
            "probe_repairs": sum(1 for c in calls if c.kind == "probe_repair"),
            "prompt_tokens": sum(c.prompt_tokens for c in calls),
            "completion_tokens": sum(c.completion_tokens for c in calls),
            "latency_s": round(sum(c.latency_s for c in calls), 2),
            # A blocked *proposal*, not a failed last call: a probe that could
            # not run is logged ok=False and the proposal still went to trial.
            "blocked": any(c.kind == "blocked" for c in calls),
            "models_served": sorted({c.model_served for c in calls
                                     if c.model_served}),
            "reasoning_tokens": sum(c.reasoning_tokens for c in calls),
            "truncated": any(c.finish_reason == "length" for c in calls),
        }

    # -- the one method the loop calls -------------------------------------- #
    def propose(self, history: Sequence[dict]) -> str:
        self._n += 1
        deadline = time.monotonic() + self.proposal_wall_clock_s
        shown = history if self.show_history else ()
        try:
            source = self._one(self._prompt(shown, repair=""), "propose")
        except ModelSubstituted:
            raise                       # a harness failure; never a trial
        except LLMError as exc:
            return self._blocked("call_failed", str(exc))

        for attempt in range(self.max_repair_attempts + 1):
            ok, message = self._static_ok(source)
            if ok:
                return self._after_static(shown, source, deadline)
            if attempt == self.max_repair_attempts:
                return self._blocked(
                    "static_check_unrepaired",
                    f"{self.max_repair_attempts} repair attempts, last: {message}")
            if time.monotonic() > deadline:
                return self._blocked("proposal_wall_clock", message)
            repair = (
                "Your previous file did not pass the static check, which runs "
                "`compile()`, an AST pass and mypy against the Strategy protocol "
                "and executes nothing. It reports no score and touches no market "
                "data. Fix exactly this and return the whole file again:\n\n"
                f"{message}\n\nPrevious file:\n\n```python\n{source}\n```"
            )
            try:
                source = self._one(self._prompt(shown, repair=repair), "repair")
            except ModelSubstituted:
                raise
            except LLMError as exc:
                return self._blocked("repair_call_failed", str(exc))
        return self._blocked("unreachable", "")   # pragma: no cover

    # -- the synthetic probe (Amendment 7) ---------------------------------- #
    def _after_static(self, shown: Sequence[dict], source: str,
                      deadline: float) -> str:
        """Probe a statically-clean file; repair on a crash or a no-op edit.

        **The probe informs, it does not gate.** Every exit from this method
        returns a file that passed the static check, and the trial runs on it.
        A probe that cannot run, a repair call that fails, a repair that breaks
        the static check, an exhausted budget -- each leaves the last good file
        standing and lets the trial proceed. Nothing here can turn a proposal
        into a blocked one, so nothing here changes what counts toward N.
        """
        if self.probe is None or not self.probe_before_trial:
            return source
        for attempt in range(self.max_probe_repairs + 1):
            res = self._probe_once(source)
            if res is None or not res.ran:
                return source
            same_as_reference = self._same_as_reference(res)
            reference = self._reference_result()
            crashed_like_reference = (
                res.crashed and reference is not None and reference.crashed)
            if crashed_like_reference:
                # The file the agent was told to edit crashes on the probe too
                # -- a fit window too short for it, a feed the synthetic set
                # lacks. Repairing that would burn the budget on a fact about
                # the probe rather than about the proposal.
                return source
            if not res.crashed and not same_as_reference:
                return source
            if attempt == self.max_probe_repairs or time.monotonic() > deadline:
                return source
            repair = self._probe_repair_text(res, same_as_reference, source)
            try:
                candidate = self._one(self._prompt(shown, repair=repair),
                                      "probe_repair")
            except ModelSubstituted:
                raise
            except LLMError:
                return source
            ok, message = self._static_ok(candidate)
            if not ok:
                self._pending.append(Call(
                    kind="probe_repair", ok=False, latency_s=0.0,
                    error=f"repair discarded, failed the static check: {message}"[:300]))
                return source
            source = candidate
        return source

    def _probe_once(self, source: str) -> "ProbeResult | None":
        assert self.probe is not None
        t0 = time.monotonic()
        try:
            res = self.probe(source)
        except Exception as exc:  # noqa: BLE001 -- the probe is best-effort
            self._pending.append(Call(kind="probe", ok=False,
                                      latency_s=time.monotonic() - t0,
                                      error=f"probe failed: {exc}"[:300]))
            return None
        note = "" if (res.ran and not res.crashed) else (res.error or "crashed")[:300]
        self._pending.append(Call(kind="probe", ok=res.ran,
                                  latency_s=time.monotonic() - t0, error=note))
        return res

    def _reference_source(self) -> str:
        if self.champion is not None and self.champion.is_file():
            return self.champion.read_text()
        return (REPO_ROOT / "src" / "strategies" / "current.py").read_text()

    def _reference_result(self) -> "ProbeResult | None":
        """The reference file's probe, run once per distinct reference."""
        assert self.probe is not None
        text = self._reference_source()
        key = hashlib.sha256(text.encode()).hexdigest()
        if key not in self._reference_probe:
            try:
                self._reference_probe[key] = self.probe(text)
            except Exception:  # noqa: BLE001
                self._reference_probe[key] = None
        return self._reference_probe[key]

    def _same_as_reference(self, res: "ProbeResult") -> bool:
        ref = self._reference_result()
        return bool(ref is not None and ref.ran and not ref.crashed
                    and res.fill_log_digest and ref.fill_log_digest
                    and res.fill_log_digest == ref.fill_log_digest)

    @staticmethod
    def _probe_repair_text(res: "ProbeResult", same_as_reference: bool,
                           source: str) -> str:
        """What the agent is told. Operational facts about its own file on a
        synthetic series: a traceback, a trade count, a byte-identity. No score,
        no P&L -- `ProbeResult` has no field for either."""
        head = (
            "Before it is run for real, your file was executed once on a short "
            "SYNTHETIC dataset: seeded noise with no relationship to the "
            "instrument you are being scored on. It reports no score. It "
            "reports only whether the file ran, how many trades it made there, "
            "and whether its fills were identical to the current strategy's.\n\n"
        )
        if res.crashed:
            body = (
                f"It CRASHED on the synthetic run ({res.profile} profile): "
                f"{res.error}\n\n"
                + (f"Last lines of the traceback:\n\n```\n{res.traceback_tail}\n```\n\n"
                   if res.traceback_tail else "")
                + "If this is a genuine fault, fix exactly it and return the whole "
                  "file again. If it can only happen on a dataset this short, or on "
                  "a feed the synthetic data does not carry, return the file "
                  "unchanged.\n\n"
            )
        else:
            body = (
                f"It ran ({res.trade_count} trades on the synthetic sessions) and "
                "produced a fill log BYTE-IDENTICAL to the current strategy's. "
                "Your edit changed nothing the engine could see there. If the "
                "change can only take effect under conditions the synthetic data "
                "cannot produce, return the file unchanged; otherwise revise it so "
                "the change actually reaches the orders.\n\n"
            )
        return head + body + f"Previous file:\n\n```python\n{source}\n```"

    # -- internals ----------------------------------------------------------- #
    def _blocked(self, reason: str, detail: str) -> str:
        self._pending.append(Call(kind="blocked", ok=False, latency_s=0.0,
                                  error=f"{reason}: {detail}"[:400]))
        return BLOCKED_TEMPLATE.format(
            reason=reason, detail=detail.replace('"""', "'''")[:800])

    def _static_ok(self, source: str) -> tuple[bool, str]:
        """`scripts/agent_check.py`'s check, on a scratch copy. Executes nothing.

        Deliberately not run against `src/strategies/current.py`: the campaign
        restores that file after every trial, and a check that wrote to it would
        race the trial it is about to become.
        """
        from ..checks import static_check, type_check

        # Unique per call: the control arm generates its proposals in parallel
        # (it is shown no history, so the order it generates in cannot matter),
        # and a shared scratch name would have two workers type-checking each
        # other's file.
        stem = f"_proposal_{os.getpid()}_{threading.get_ident():x}"
        scratch = REPO_ROOT / "src" / "strategies" / f"{stem}.py"
        report = static_check(scratch, source=source)
        errors = [str(f) for f in report.findings if f.code.startswith("E")]
        if errors or not report.strategy_class:
            return (not errors), "; ".join(errors[:6])
        # mypy needs a file, and the conformance stub it generates imports the
        # module by name -- so the scratch has to live where that name resolves.
        # `src/strategies/current.py` is not available: the campaign restores it
        # after each trial and writing there during a proposal would race the
        # trial this proposal is about to become.
        scratch.write_text(source)
        try:
            errors.extend(type_check(scratch, report.strategy_class,
                                     module=f"src.strategies.{stem}").errors)
        finally:
            scratch.unlink(missing_ok=True)
        return (not errors), "; ".join(errors[:6])

    def _prompt(self, history: Sequence[dict], *, repair: str) -> str:
        if self.champion is not None and self.champion.is_file():
            current = self.champion.read_text()
        else:
            current = (REPO_ROOT / "src" / "strategies" / "current.py").read_text()
        return self._template.format(
            api=self._docs["api"],
            context_api=self._docs["context_api"],
            data=self._docs["data"],
            run=self.run_facts or "(not supplied)",
            hypothesis=hypothesis_block(self.hypothesis),
            current=current,
            history=self._history_block(history),
            repair=repair,
        )

    def _history_block(self, history: Sequence[dict]) -> str:
        if not self.show_history:
            return ("Withheld. This is the control arm of the dry run: you are "
                    "shown nothing about your own previous proposals, so that "
                    "the campaign's trajectory can be compared against one that "
                    "is. Propose on the interface alone.")
        if not history:
            return "None yet. This is the first proposal of the campaign."

        # A whitelist. The objective, and the operational facts needed to
        # iterate -- not the diagnostics, and under no circumstances anything
        # the correction computes. A blacklist over a row shape that grows
        # leaks the first field somebody adds.
        lines, sources = [], []
        # **Tell the agent when a proposal traded identically to an earlier one.**
        # nq-ml-02 produced two such pairs -- 146 and 172 changed source lines,
        # byte-identical fill logs, the same score to four decimals. The agent
        # narrated changes that never took effect and spent two of twenty trials
        # re-scoring known candidates, because nothing told it. This is
        # operational fact about its own history, not the gate: no p, no verdict,
        # no N, no correction. The whitelist above stands.
        first_seen: dict[str, int] = {}
        for i, row in enumerate(history):
            score = row.get("score") or {}
            primary = score.get("primary")
            bits = [f"#{i}", f"outcome={row.get('outcome')}"]
            if primary is not None and not score.get("rejected"):
                bits.append(f"score={primary:+.4f}")
                bits.append(f"trades={score.get('trade_count')}")
                bits.append(f"sessions={score.get('n_sessions')}")
            elif score.get("rejected"):
                bits.append(f"NOT SCORED: {score.get('reject_reason')}")
            else:
                bits.append(f"NO SCORE: {str(row.get('error'))[:120]}")
            digest = row.get("fill_log_digest")
            if digest:
                if digest in first_seen:
                    bits.append(f"IDENTICAL TRADES TO #{first_seen[digest]} "
                                "-- your edit changed nothing the engine could see")
                else:
                    first_seen[digest] = i
            lines.append("  ".join(bits))

        scored = [(i, r) for i, r in enumerate(history)
                  if (r.get("score") or {}).get("primary") is not None
                  and not (r.get("score") or {}).get("rejected")]
        picks: list[int] = []
        if scored:
            picks.append(max(scored, key=lambda p: p[1]["score"]["primary"])[0])
        picks.extend(range(max(0, len(history) - self.history_full_sources),
                           len(history)))
        for i in sorted(set(picks)):
            src = _source_of(history[i])
            if src:
                tag = "best so far" if scored and i == picks[0] else "recent"
                sources.append(f"#### #{i} ({tag})\n\n```python\n{src}\n```")

        out = ["Every proposal you have made in this campaign, in order:", "",
               "```", *lines, "```"]
        if sources:
            out += ["", "Full source of the ones most worth looking at:", "",
                    *sources]
        return "\n".join(out)

    def _same_model(self, served: str) -> bool:
        """Endpoints decorate ids -- a provider prefix, a deployment suffix, a
        dated snapshot. Substitution means a *different model*, not a different
        spelling, so the comparison is on the last path segment and either may
        be a prefix of the other."""
        a = self.model.rsplit("/", 1)[-1].lower()
        b = served.rsplit("/", 1)[-1].lower()
        return a.startswith(b) or b.startswith(a)

    def preflight(self) -> dict:
        """One cheap call, to find out what the endpoint is actually serving.

        Run before a campaign is registered and before one starts. Raises
        `ModelSubstituted` if the answer is not the model that will be written
        into the record.
        """
        import requests

        # **`max_tokens` has to cover the model's reasoning, not just its
        # answer.** At 16 the probe failed on every reasoning model: the budget
        # is spent before a visible token is emitted, and the gateway reports
        # the empty output as `ContentPolicyViolationError: Response output was
        # blocked` -- which reads like a refusal and is a truncation. Measured
        # 2026-09-25 on `gemini-3.1-pro-preview`: 4 of 4 failures at 16, 200 and
        # an honest `model` field at 64, with temperature making no difference.
        # This is the same defect the committee client hit at 4,096 against
        # deepseek's ~7k of `reasoning_content`, in a smaller place, and it made
        # every reasoning model unregistrable.
        #
        # 512 rather than 64: the margin is free on a one-token answer, and the
        # point of this call is to read the `model` field rather than to be
        # cheap.
        # **Probed at the size the campaign will really ask for, not at 512.**
        # `max_tokens` is a cap, not a charge -- the answer is two tokens
        # either way -- but it is part of what the endpoint routes on. A 512
        # probe said `claude-haiku-4-5` while every 65,536 proposal was being
        # answered by `gemini-3.1-pro-preview`, so the guard whose whole job is
        # to catch substitution vouched for a run it had not tested. Floored at
        # PREFLIGHT_FLOOR_TOKENS because a reasoning model spends its budget
        # visible token, and an agent configured below that would truncate.
        body = {"model": self.model,
                "max_tokens": max(self.max_tokens, PREFLIGHT_FLOOR_TOKENS),
                "temperature": 0, "cache": NO_CACHE,
                "messages": [{"role": "user", "content": "reply with: ok"}]}
        resp = requests.post(
            f"{self.base_url}/chat/completions", json=body,
            headers={"Authorization": f"Bearer {os.environ['LITELLM_API_KEY']}"},
            timeout=self.request_timeout_s)
        if resp.status_code != 200:
            raise LLMError(
                f"the endpoint will not serve {self.model!r}: HTTP "
                f"{resp.status_code}. {resp.text[:200]}")
        served = str(resp.json().get("model") or "")
        if not served:
            # **An endpoint that does not say what it served cannot be checked,
            # and "cannot be checked" is not "fine".** This used to fall through
            # to `return`, so the one configuration where a substitution is
            # invisible -- a reply with no `model` field -- was the one that
            # passed silently. The gateway carries a wildcard fallback,
            # `fallbacks=[{'*': ['gemini-3.1-pro-preview']}]`, so that is not a
            # hypothetical shape: it is exactly what Task 37 cost 35 trials to
            # discover, in the form that would not have raised.
            raise ModelSubstituted(
                f"the endpoint answered for {self.model!r} without naming a "
                "model, so substitution cannot be ruled out. The gateway has a "
                "wildcard fallback configured; an unverifiable reply is treated "
                "as a substitution rather than as a pass.")
        if not self._same_model(served):
            raise ModelSubstituted(
                f"asked for {self.model!r}, endpoint answered {served!r}")
        return {"requested": self.model, "served": served}

    def _one(self, prompt: str, kind: str) -> str:
        """One proposal, retried only when the endpoint never answered.

        **What is and is not a retry here.** A reply we dislike is final: asking
        again would be a re-roll, and the correction is over the proposals we
        made rather than the ones we kept. A transport failure is not a reply at
        all -- see `LLMTransportError` -- so retrying it changes nothing about N
        and recovers a trial that would otherwise be spent on a placeholder that
        raises at module level.

        Measured 2026-10-01 before this existed: **24 of 503 trials** project-wide
        were consumed by `agent_blocked: call_failed`, every one a gateway
        `ReadTimeout`, and three of them inside one 20-trial campaign. Each
        attempt is recorded in the call log, so the record shows the retries
        rather than hiding them.
        """
        last: Exception | None = None
        for attempt in range(MAX_TRANSPORT_RETRIES + 1):
            try:
                return self._one_attempt(prompt, kind)
            except LLMTransportError as exc:
                last = exc
                if attempt == MAX_TRANSPORT_RETRIES:
                    break
                wait = TRANSPORT_BACKOFF_S[min(attempt,
                                               len(TRANSPORT_BACKOFF_S) - 1)]
                time.sleep(wait)
        raise LLMError(f"{last}") from None

    def _one_attempt(self, prompt: str, kind: str) -> str:
        """One call. One. Its failure is for `_one` to classify."""
        import requests

        system, user = prompt.split("## USER", 1)
        body = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "cache": NO_CACHE,
            "messages": [
                {"role": "system", "content": system.split("## SYSTEM", 1)[-1].strip()},
                {"role": "user", "content": user.strip()},
            ],
        }
        if self.seed is not None:
            body["seed"] = self.seed

        t0 = time.monotonic()
        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions", json=body,
                headers={"Authorization": f"Bearer {os.environ['LITELLM_API_KEY']}"},
                timeout=self.request_timeout_s,
            )
        except Exception as exc:
            self._pending.append(Call(kind, False, time.monotonic() - t0,
                                      error=f"{type(exc).__name__}: {exc}"[:300]))
            # requests raised: timeout, DNS, refused, reset. No reply exists.
            raise LLMTransportError(f"{type(exc).__name__}: {exc}") from None
        latency = time.monotonic() - t0

        if resp.status_code != 200:
            self._pending.append(Call(kind, False, latency,
                                      error=f"HTTP {resp.status_code}"))
            # 429 and 5xx mean "ask again"; 400 and 401 mean "you are wrong",
            # and re-asking those just burns the budget on the same mistake.
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                raise LLMTransportError(
                    f"HTTP {resp.status_code}: {resp.text[:200]}")
            raise LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            payload = resp.json()
            usage = payload.get("usage") or {}
            choice = payload["choices"][0]
            text = choice["message"]["content"] or ""
        except Exception as exc:
            self._pending.append(Call(kind, False, latency,
                                      error=f"unparseable envelope: {exc}"[:300]))
            raise LLMError(f"unparseable response envelope: {exc}") from None

        served = str(payload.get("model") or "")
        if served and not self._same_model(served):
            # Before anything else, including before the reply is parsed. A
            # substituted model is not a proposal that went wrong; it is the
            # experiment not having happened.
            raise ModelSubstituted(
                f"asked the endpoint for {self.model!r} and it answered with "
                f"{served!r}. The campaign record says {self.model!r}, so "
                "continuing would write a record that is false. Stop, wait for "
                "the endpoint, and discard whatever has run."
            )
        details = usage.get("completion_tokens_details") or {}
        finish = str(choice.get("finish_reason") or "")
        call = Call(kind, True, latency,
                    prompt_tokens=int(usage.get("prompt_tokens") or 0),
                    completion_tokens=int(usage.get("completion_tokens") or 0),
                    model_served=served,
                    finish_reason=finish,
                    reasoning_tokens=int(details.get("reasoning_tokens") or 0))
        source = extract_source(text)
        if source is None:
            # Silence and noise are the same event. An empty reply, prose with
            # no fence, a fence with nothing in it -- none of them is a
            # strategy, and none of them is a reason to ask again.
            self._pending.append(Call(
                kind, False, latency,
                prompt_tokens=call.prompt_tokens,
                completion_tokens=call.completion_tokens,
                model_served=served, finish_reason=finish,
                reasoning_tokens=call.reasoning_tokens,
                error=(f"no python block in the reply "
                       f"(finish_reason={finish}, "
                       f"reasoning_tokens={call.reasoning_tokens}, "
                       f"completion_tokens={call.completion_tokens}, "
                       f"max_tokens={self.max_tokens})")))
            raise LLMError(
                f"no python block in a {len(text)}-character reply: "
                f"finish_reason={finish}, reasoning_tokens="
                f"{call.reasoning_tokens}, completion_tokens="
                f"{call.completion_tokens} of max_tokens={self.max_tokens}")
        self._pending.append(call)
        return source


def extract_source(text: str) -> str | None:
    """The fenced block, or `None`. `None` is a blocked proposal, not a retry.

    Takes the **last** fenced block rather than the first: a model that thinks
    out loud before answering puts the answer last, and a model that answers
    first and then explains has nothing after the fence to confuse this.
    """
    if not text or not text.strip():
        return None
    blocks = [b for b in FENCE.findall(text) if b.strip()]
    if blocks:
        return blocks[-1].strip() + "\n"
    # No fence at all. Accept it only if the whole reply is plausibly a module;
    # anything else is prose, and prose written to a .py file is a syntax error
    # dressed up as a trial.
    stripped = text.strip()
    if stripped.startswith(("import ", "from ", "class ", '"""', "#")):
        return stripped + "\n"
    return None


def _source_of(row: dict) -> str:
    """The stored source for a ledger row. Content-addressed since Task 20."""
    path = row.get("strategy_source")
    if not path:
        return ""
    p = Path(path)
    return p.read_text() if p.exists() else ""
