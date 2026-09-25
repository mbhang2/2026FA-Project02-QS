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
from typing import Sequence

from .hypothesis import Hypothesis, block as hypothesis_block

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

    kind: str                 # "propose" | "repair"
    ok: bool
    latency_s: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str = ""
    # What the endpoint says it answered with. `config()["model"]` is what was
    # asked for, and a gateway is free to serve something else.
    model_served: str = ""

    def as_dict(self) -> dict:
        return {"kind": self.kind, "ok": self.ok,
                "latency_s": round(self.latency_s, 2),
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "model_served": self.model_served,
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
    request_timeout_s: float = 240.0
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
    # Everything below is state, not configuration.
    calls: list[Call] = field(default_factory=list, init=False, repr=False)
    _pending: list[Call] = field(default_factory=list, init=False, repr=False)
    _repairs: int = field(default=0, init=False, repr=False)
    _n: int = field(default=0, init=False, repr=False)

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
            "prompt_tokens": sum(c.prompt_tokens for c in calls),
            "completion_tokens": sum(c.completion_tokens for c in calls),
            "latency_s": round(sum(c.latency_s for c in calls), 2),
            "blocked": bool(calls) and not calls[-1].ok,
            "models_served": sorted({c.model_served for c in calls
                                     if c.model_served}),
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
                return source
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

        body = {"model": self.model, "max_tokens": 16, "temperature": 0,
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
        """One call. One. A failure here is a blocked proposal, never a retry."""
        import requests

        system, user = prompt.split("## USER", 1)
        body = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
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
            raise LLMError(f"{type(exc).__name__}: {exc}") from None
        latency = time.monotonic() - t0

        if resp.status_code != 200:
            self._pending.append(Call(kind, False, latency,
                                      error=f"HTTP {resp.status_code}"))
            raise LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            payload = resp.json()
            usage = payload.get("usage") or {}
            text = payload["choices"][0]["message"]["content"] or ""
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
        call = Call(kind, True, latency,
                    prompt_tokens=int(usage.get("prompt_tokens") or 0),
                    completion_tokens=int(usage.get("completion_tokens") or 0),
                    model_served=served)
        source = extract_source(text)
        if source is None:
            # Silence and noise are the same event. An empty reply, prose with
            # no fence, a fence with nothing in it -- none of them is a
            # strategy, and none of them is a reason to ask again.
            self._pending.append(Call(kind, False, latency,
                                      prompt_tokens=call.prompt_tokens,
                                      completion_tokens=call.completion_tokens,
                                      error="no python block in the reply"))
            raise LLMError(
                f"no python block in a {len(text)}-character reply")
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
