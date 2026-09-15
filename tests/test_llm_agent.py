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

    os.environ.setdefault("LITELLM_BASE_URL", "https://example.invalid/v1")
    os.environ.setdefault("LITELLM_API_KEY", "not-a-key-and-never-sent")
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
    assert fields == {"api", "context_api", "data", "run", "current",
                      "history", "repair"}, fields
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
