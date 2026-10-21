"""The proposing agent, offline. No network is touched by anything in here.

Two properties carry most of the weight and both are about what the agent is
*not* shown. The first is that the prompt and the history block contain nothing
about the gate -- an agent shown a threshold optimizes the threshold, and the
correction's guarantee is then about a search that already knew the answer. The
second is that a proposal which cannot be turned into a strategy still becomes a
trial: silence, prose, an empty reply and an unrepairable file are the same
event, and that event is a row that counts toward N.

The rest pin the pre-registration surface, because a decision recorded only in
prose is a decision that drifts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.core.loop import llm  # noqa: E402

# Vocabulary the agent must never see. Deliberately over-broad: a false positive
# costs one reworded sentence, a false negative costs the campaign's guarantee.
#
# Bare "threshold" is **not** on the list, and the omission is deliberate rather
# than convenient. `strategy-api.md` uses it twice to describe the Task 23
# oracle attack -- a warning to the agent, which it should read -- and four
# times as the name of a parameter in a worked example. Banning the word would
# ban the warning. What must not appear is a threshold *the gate uses*, and the
# specific vocabulary below is what that is made of.
GATE_WORDS = (
    "p-value", "p value", "false-acceptance", "false acceptance",
    "benjamini", "hochberg", "holm", "westfall", "fwer", "fdr",
    "multiple testing", "multiple-testing", "multiple-comparison",
    "accept/reject", "rejected by the gate", "deflated sharpe",
    "null distribution", "bonferroni", "family-wise", "familywise",
)


def _agent(**kw):
    """An agent with no endpoint call in its future. `__post_init__` insists on
    the environment variables, so the test supplies them rather than the shell:
    a test that only runs when somebody's credentials happen to be loaded is a
    test that does not run in CI."""
    import os

    # **Assigned, not `setdefault`.** `setdefault` defers to the shell, which
    # is the opposite of what the docstring above promises: with a live
    # LITELLM_BASE_URL exported, these agents pointed at the REAL gateway.
    # Measured 2026-10-20 once the gateway came back -- the suite went from
    # 136 s to >10 min at 7% CPU, sleeping through TRANSPORT_BACKOFF_S
    # (30, 120, 300, 600, 600) on retries against a live endpoint. A test that
    # behaves differently depending on who is running it is not a test, and
    # one that can spend real tokens is worse. example.invalid cannot resolve,
    # so leaking these values to later tests fails closed.
    os.environ["LITELLM_BASE_URL"] = "https://example.invalid/v1"
    os.environ["LITELLM_API_KEY"] = "not-a-key-and-never-sent"
    return llm.LLMAgent(model="test-model", **kw)


# --------------------------------------------------------------------------- #
# what the agent is not shown
# --------------------------------------------------------------------------- #
def _messages(agent, history=()) -> tuple[str, str]:
    """The two strings that actually go on the wire, assembled the way `_one`
    assembles them. Testing the file instead of these would test the wrong
    thing: the template opens with a comment *stating* the prohibition, and
    that comment is above the `## SYSTEM` marker and never sent."""
    prompt = agent._prompt(history, repair="")
    system, user = prompt.split("## USER", 1)
    return system.split("## SYSTEM", 1)[-1].strip(), user.strip()


def test_the_suite_never_points_an_agent_at_a_real_endpoint(monkeypatch):
    """A live LITELLM_BASE_URL in the shell must not reach the agent.

    This used to be `os.environ.setdefault`, so whoever had the gateway
    exported ran these tests against it: real calls, real tokens, and retries
    that sleep through TRANSPORT_BACKOFF_S. The suite took >10 minutes at 7%
    CPU on 2026-10-20 before anyone noticed it was not computing.
    """
    monkeypatch.setenv("LITELLM_BASE_URL", "https://gateway.example.com/v1")
    monkeypatch.setenv("LITELLM_API_KEY", "sk-looks-real")
    assert _agent().base_url == "https://example.invalid/v1"


def test_nothing_sent_to_the_agent_mentions_the_gate():
    system, user = _messages(_agent())
    text = (system + "\n" + user).lower()
    found = [w for w in GATE_WORDS if w in text]
    assert not found, (
        f"the assembled prompt mentions {found}. The agent optimizes the "
        "objective; the gate is applied afterwards over the campaign. Showing "
        "it the gate makes the correction a statement about a search that "
        "already knew the bar."
    )


def test_the_templates_own_prohibition_is_not_sent_to_the_agent():
    """The comment at the top of the template says what may not appear in it,
    using the words it forbids. It has to stay on this side of the wire."""
    system, user = _messages(_agent())
    assert "NOTHING HERE MAY MENTION THE GATE" not in system + user
    assert "<!--" not in system + user
    assert "You are a quantitative researcher" in system


def test_the_history_block_shows_the_score_and_nothing_the_correction_computes():
    rows = [
        {"outcome": "completed", "campaign_index": 0,
         "score": {"primary": 0.42, "trade_count": 17, "n_sessions": 105,
                   "rejected": False, "sharpe": 9.9, "skew": -3.3,
                   "kurtosis": 88.0, "significance": 4.4},
         # Fields a future row might carry. A blacklist would leak every one of
         # them; the whitelist is what makes this test pass by construction.
         "far": 0.0004, "bh_critical": 0.0005, "gate_verdict": "ACCEPTED",
         "corrected_p": 0.03, "campaign_n": 100},
    ]
    block = _agent(show_history=True)._history_block(rows)
    assert "+0.4200" in block, "the objective is the whole point of showing it"
    assert "trades=17" in block
    for leaked in ("0.0004", "0.0005", "ACCEPTED", "0.03", "9.9", "-3.3", "88.0"):
        assert leaked not in block, f"{leaked!r} reached the agent"


def test_the_control_arm_is_told_nothing_about_its_past():
    rows = [{"outcome": "completed", "campaign_index": 0,
             "score": {"primary": 0.42, "trade_count": 1, "n_sessions": 105,
                       "rejected": False}}]
    block = _agent(show_history=False)._history_block(rows)
    assert "0.42" not in block and "#0" not in block
    assert "control" in block.lower()


def test_choose_null_style_enforcement_the_config_asserts_it():
    cfg = _agent().config()
    assert cfg["shows_the_gate"] is False
    assert cfg["retries_allowed"] is False


# --------------------------------------------------------------------------- #
# silence, noise and an unrepairable file are all the same event
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", [
    "", "   \n\n ", "Here is a strategy that buys when the market goes up.",
    "I cannot help with that.", "```\n\n```", "```python\n\n```",
])
def test_an_unusable_reply_yields_no_source(text):
    assert llm.extract_source(text) is None


@pytest.mark.parametrize("text,expected", [
    ("```python\nclass Strategy:\n    pass\n```", "class Strategy:\n    pass\n"),
    ("prose\n```py\nx = 1\n```\nmore", "x = 1\n"),
    # Reasoning first, answer last: take the last block, not the first.
    ("```python\nbad = 1\n```\nactually:\n```python\ngood = 2\n```",
     "good = 2\n"),
    ("import os\n\nclass Strategy:\n    pass", "import os\n\nclass Strategy:\n    pass\n"),
])
def test_a_usable_reply_yields_its_last_block(text, expected):
    assert llm.extract_source(text) == expected


def test_a_blocked_proposal_is_a_file_that_raises_rather_than_a_skipped_trial():
    source = _agent()._blocked("call_failed", "HTTP 500")
    # It must be a *valid module that raises*, not a syntax error: the point is
    # that `run.py` loads it, fails, and records a crashed trial with a message
    # a reader can act on.
    compile(source, "current.py", "exec")
    with pytest.raises(RuntimeError, match="agent_blocked: call_failed"):
        exec(compile(source, "current.py", "exec"), {"__name__": "current"})
    assert "counts toward N" in source


def test_a_blocked_proposal_is_logged_as_a_failed_call():
    agent = _agent()
    agent._blocked("static_check_unrepaired", "E001 no Strategy class")
    log = agent.drain_log()
    assert log["blocked"] is True
    assert "static_check_unrepaired" in log["calls"][0]["error"]
    assert agent.drain_log()["n_calls"] == 0, "the log drains once per trial"


def test_a_reply_that_triple_quotes_cannot_break_out_of_the_blocked_docstring():
    source = _agent()._blocked("call_failed", 'oops """ raise SystemExit()')
    compile(source, "current.py", "exec")


# --------------------------------------------------------------------------- #
# the pre-registration surface
# --------------------------------------------------------------------------- #
def test_the_config_pins_everything_a_nondeterministic_agent_needs_pinned():
    cfg = _agent(temperature=0.3, max_tokens=999, show_history=False).config()
    for key in ("model", "temperature", "max_tokens", "seed", "endpoint",
                "prompt_template", "prompt_template_digest", "documents_shown",
                "show_history", "max_repair_attempts", "request_timeout_s",
                "proposal_wall_clock_s", "retries_allowed", "shows_the_gate"):
        assert key in cfg, key
    assert cfg["temperature"] == 0.3 and cfg["show_history"] is False
    assert json.dumps(cfg, sort_keys=True), "the record has to serialise it"
    assert "not-a-key" not in json.dumps(cfg), "the key never reaches a record"


def test_the_template_digest_tracks_the_template():
    assert llm.template_digest() == llm.template_digest()
    assert _agent().config()["prompt_template_digest"] == llm.template_digest()


def test_the_prompt_template_is_in_the_artifact_fingerprint_set():
    """It shaped every score in a campaign that used it, so a mid-run edit has
    to invalidate the trial the way a rewritten null table does."""
    from src.core.scoring import integrity

    digests = integrity.artifact_digests()
    rel = str(llm.TEMPLATE_PATH.relative_to(REPO_ROOT))
    assert rel in digests, (
        f"{rel} is not in ARTIFACT_GLOBS. A prompt edited between two "
        "campaigns whose results are compared is the same defect as a null "
        "table edited between them."
    )


def test_every_placeholder_the_template_declares_is_supplied():
    """A missing one is a KeyError at assembly, before any call is billed."""
    import string

    fields = {f for _, f, _, _ in string.Formatter().parse(
        llm.TEMPLATE_PATH.read_text()) if f}
    prompt = _agent()._prompt((), repair="")
    assert fields == {"api", "context_api", "data", "run", "hypothesis",
                      "current", "history", "repair"}, fields
    assert len(prompt) > 10_000, "the documents did not make it into the prompt"


def test_the_prompt_splits_into_exactly_one_system_and_one_user_part():
    """`_one` splits on the markers, so a document containing one would move
    text across the boundary silently."""
    prompt = _agent()._prompt((), repair="")
    assert prompt.count("\n## SYSTEM") == 1
    assert prompt.count("\n## USER") == 1


# --------------------------------------------------------------------------- #
# the static check under concurrency
# --------------------------------------------------------------------------- #
def test_the_static_check_survives_being_called_from_several_threads():
    """The control arm generates its proposals concurrently, so `type_check`
    runs from several threads at once. mypy's API keeps process-global state
    and answers `INTERNAL ERROR ... _hashlib.pyi:26` when it is called that way
    -- which `type_check` reports as a finding, which sends the agent away to
    repair a file that was fine. A harness failure wearing a candidate
    failure's clothes, on the one path whose job is to keep those apart.
    """
    from concurrent.futures import ThreadPoolExecutor

    good = (REPO_ROOT / "src" / "strategies" / "current.py").read_text()
    agent = _agent()
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: agent._static_ok(good), range(12)))
    bad = [msg for ok, msg in results if not ok]
    assert not bad, (
        "the committed strategy failed its own static check under concurrency: "
        f"{bad[:2]}"
    )


# --------------------------------------------------------------------------- #
# the model that answered, versus the model that was asked for
# --------------------------------------------------------------------------- #
def test_a_substituted_model_is_a_harness_failure_not_a_blocked_proposal():
    """A blocked proposal is a trial and counts toward N. A substituted model is
    not a proposal that went wrong -- it is the experiment not having happened,
    and a campaign whose record names one model while its trials ran on another
    is a record that is false in a way no reader could detect.

    Task 37 found this live: `claude-haiku-4-5` started returning 503 and the
    gateway answered 200 with `gemini-3.1-pro-preview`.
    """
    agent = _agent()
    agent.model = "claude-haiku-4-5"
    assert agent._same_model("claude-haiku-4-5")
    assert agent._same_model("anthropic/claude-haiku-4-5")
    assert agent._same_model("claude-haiku-4-5-20251001")   # dated snapshot
    assert not agent._same_model("gemini-3.1-pro-preview")
    assert not agent._same_model("claude-sonnet-5")

    with pytest.raises(llm.ModelSubstituted):
        raise llm.ModelSubstituted("asked for x, got y")
    assert not issubclass(llm.ModelSubstituted, llm.LLMError), (
        "it must not be caught by the `except LLMError` that turns a failed "
        "call into a blocked proposal -- that would charge a harness failure "
        "to N"
    )


def test_every_call_records_the_model_that_answered_it():
    c = llm.Call("propose", True, 1.0, model_served="gemini-3.1-pro-preview")
    assert c.as_dict()["model_served"] == "gemini-3.1-pro-preview"
    agent = _agent()
    agent._pending.append(c)
    assert agent.drain_log()["models_served"] == ["gemini-3.1-pro-preview"]


def test_the_history_names_a_proposal_that_traded_identically_to_an_earlier_one():
    """nq-ml-02: trials 2 and 16 differed by 146 source lines and had
    byte-identical fill logs. The agent was never told, and spent the trial.
    The history line now says so. It is operational fact about the agent's own
    past, not the gate -- the gate-word whitelist is checked separately."""
    from src.core.loop.llm import LLMAgent
    a = LLMAgent.__new__(LLMAgent)
    a.history_full_sources = 0
    a.show_history = True
    rows = [
        {"outcome": "completed", "score": {"primary": 0.1, "trade_count": 5, "n_sessions": 100}, "fill_log_digest": "aaa"},
        {"outcome": "completed", "score": {"primary": 0.2, "trade_count": 7, "n_sessions": 100}, "fill_log_digest": "bbb"},
        {"outcome": "completed", "score": {"primary": 0.1, "trade_count": 5, "n_sessions": 100}, "fill_log_digest": "aaa"},
    ]
    text = a._history_block(rows)
    l0, l1, l2 = [l for l in text.splitlines() if l.strip().startswith("#")]
    assert "IDENTICAL TRADES TO #0" in l2
    assert "IDENTICAL" not in l0 and "IDENTICAL" not in l1
    for gate_word in ("p=", "p-value", "verdict", "correction", "accept", "reject-the-null"):
        assert gate_word not in text.lower(), gate_word


def test_the_preflight_budget_covers_a_reasoning_models_thinking():
    """**Regression, 2026-09-25: the preflight could not register any reasoning
    model.** It sent `max_tokens: 16`, which such a model spends before emitting
    a visible token; the gateway then reports the empty output as
    `ContentPolicyViolationError: Response output was blocked`, which reads like
    a refusal and is a truncation. Measured on `gemini-3.1-pro-preview`: 4 of 4
    failures at 16, and 200 with an honest `model` field at 64.

    The same shape as the committee client's `max_tokens=4096` against
    deepseek's ~7k of `reasoning_content`, which returned `finish_reason=length`
    and empty content on three of four seats of nq-ml-02.
    """
    import inspect

    src = inspect.getsource(llm.LLMAgent.preflight)
    assert '"max_tokens": 16,' not in src
    budget = int(src.split('"max_tokens":')[1].split(",")[0].strip())
    assert budget >= 256, (
        f"the preflight asks for {budget} tokens; a reasoning model spends more "
        "than that before its first visible token, and the failure arrives "
        "disguised as a content-policy refusal")


def test_a_reply_with_no_code_block_says_whether_it_was_truncated():
    """**nq-ml-05 lost 4 of its first 10 trials and the log could not say why.**

    "no python block in a 1865-character reply" is the same message whether the
    model refused, rambled, or ran out of budget mid-fence. The third is what
    was happening -- gemini-3.1-pro-preview spends ~12k tokens reasoning and the
    budget was 16,384 -- and diagnosing it needed a call reproduced by hand.
    `finish_reason` and `reasoning_tokens` are what distinguish them, the
    committee client has recorded both since nq-ml-02, and now so does this.
    """
    c = llm.Call("propose", False, 1.0, completion_tokens=16384,
                 finish_reason="length", reasoning_tokens=12004,
                 error="no python block in the reply (finish_reason=length, "
                       "reasoning_tokens=12004)")
    d = c.as_dict()
    assert d["finish_reason"] == "length" and d["reasoning_tokens"] == 12004

    agent = _agent()
    agent._pending.append(c)
    log = agent.drain_log()
    assert log["truncated"] is True, (
        "a trial whose proposal was cut off mid-reply has to be visible in the "
        "sidecar without reproducing the call")
    assert log["reasoning_tokens"] == 12004


def test_the_proposer_budget_clears_a_reasoning_models_appetite():
    """16,384 was not enough for a model that thinks for 12,000 tokens."""
    import re

    src = (REPO_ROOT / "scripts" / "nq_campaign.py").read_text()
    m = re.search(r"PROPOSER_MAX_TOKENS = ([\d_]+)", src)
    assert m, "the budget must be a named constant, not a literal at two call sites"
    assert int(m.group(1).replace("_", "")) >= 32_768
    assert "max_tokens=16_384" not in src


# --------------------------------------------------------------------------- #
# a failed CALL and a bad PROPOSAL are different events
# --------------------------------------------------------------------------- #
def _attempts(agent, monkeypatch, outcomes):
    """Drive `_one` with a scripted sequence of `_one_attempt` outcomes."""
    seen = []

    def fake(prompt, kind):
        seen.append(kind)
        out = outcomes[len(seen) - 1]
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(agent, "_one_attempt", fake)
    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)
    return seen


def test_a_transport_failure_is_retried_because_it_produced_no_proposal(
        monkeypatch):
    """24 of 503 trials project-wide were spent on `agent_blocked: call_failed`,
    every one a gateway ReadTimeout. Retrying a call that never answered does not
    re-roll a proposal -- there was nothing to re-roll -- and N is unchanged."""
    agent = _agent()
    seen = _attempts(agent, monkeypatch, [
        llm.LLMTransportError("ReadTimeout"),
        llm.LLMTransportError("ReadTimeout"),
        "```python\nclass Strategy:\n    pass\n```",
    ])
    out = agent._one("## SYSTEM\ns\n## USER\nu", "propose")
    assert "class Strategy" in out
    assert len(seen) == 3, "it must have retried twice and then succeeded"


def test_a_reply_we_dislike_is_never_retried(monkeypatch):
    """The load-bearing half. Asking again for a proposal that came back is a
    re-roll, and `max_of_n` corrects over the proposals we MADE, not the ones we
    kept. Only transport is retryable."""
    agent = _agent()
    seen = _attempts(agent, monkeypatch, [llm.LLMError("HTTP 400: bad request")])
    with pytest.raises(llm.LLMError):
        agent._one("## SYSTEM\ns\n## USER\nu", "propose")
    assert len(seen) == 1, "a content failure must be final"


def test_a_substituted_model_is_not_retried_either(monkeypatch):
    """It is a harness failure, so it must propagate past the retry loop
    untouched rather than being re-asked until the gateway behaves."""
    agent = _agent()
    seen = _attempts(agent, monkeypatch,
                     [llm.ModelSubstituted("asked x, got y")])
    with pytest.raises(llm.ModelSubstituted):
        agent._one("## SYSTEM\ns\n## USER\nu", "propose")
    assert len(seen) == 1


def test_transport_retries_are_bounded_and_then_block(monkeypatch):
    agent = _agent()
    seen = _attempts(agent, monkeypatch,
                     [llm.LLMTransportError("ReadTimeout")] * 9)
    with pytest.raises(llm.LLMError):
        agent._one("## SYSTEM\ns\n## USER\nu", "propose")
    assert len(seen) == llm.MAX_TRANSPORT_RETRIES + 1


def test_transport_is_not_a_subclass_of_the_error_that_blocks_a_proposal():
    """`propose` catches LLMError and turns it into a blocked trial. If
    LLMTransportError were a subclass, the retry loop would never see it."""
    assert not issubclass(llm.LLMTransportError, llm.LLMError)
    assert not issubclass(llm.LLMError, llm.LLMTransportError)


def test_which_http_statuses_mean_ask_again():
    """429 and 5xx mean ask again; 400 and 401 mean you are wrong, and re-asking
    those burns the budget on the same mistake."""
    src = (REPO_ROOT / "src" / "core" / "loop" / "llm.py").read_text()
    assert "if resp.status_code == 429 or 500 <= resp.status_code < 600:" in src
    flat = " ".join(src.split())
    assert "mean \"ask again\"" in flat or 'mean "ask again"' in flat


def test_the_request_timeout_clears_the_measured_latency_tail():
    """Measured over 506 real calls: median 22s, p90 77s, p99 198s, max 241s --
    against a 240s limit, so the failures were sitting exactly at the ceiling
    rather than scattered."""
    import inspect
    default = inspect.signature(llm.LLMAgent).parameters["request_timeout_s"].default
    assert default >= 600.0, (
        f"the default timeout is {default}s; the measured p99 is 198s and the "
        "max 241s, and at 240s the failures were at the ceiling")


def test_every_attempt_is_recorded_rather_than_only_the_last():
    """A retried call must leave its failures in the log, or the record shows a
    clean single call where three happened."""
    src = (REPO_ROOT / "src" / "core" / "loop" / "llm.py").read_text()
    flat = " ".join(src.split())
    assert "Each attempt is recorded in the call log" in flat
    # the Call append happens inside _one_attempt, which the loop calls per try
    i = src.index("def _one_attempt")
    assert "self._pending.append" in src[i:i + 3000]
