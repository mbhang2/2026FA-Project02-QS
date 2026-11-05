"""A seat that never ran is asked again: inside the committee, and afterwards.

No network. The client is a fake that replays a script of replies, so these pin
the retry policy in `audit.committee.review._one` and the bookkeeping in
`scripts/committee_retry.py` without spending a call.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

import importlib as _il

rv = _il.import_module("audit.committee.review")   # the package re-exports a function of the same name
from audit.committee.llm import LLMUnavailable, ModelSubstituted

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _script_mod():
    spec = importlib.util.spec_from_file_location(
        "committee_retry", ROOT / "scripts" / "committee_retry.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Scripted:
    """Replays replies in order; an Exception instance is raised instead."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = 0

    def ask(self, system, user):
        self.calls += 1
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)


def _ask(client):
    return rv._one("economics", client, {}, "class Strategy: ...", "# q")


NOT_JSON = LLMUnavailable("reply was not JSON: Expecting value; finish_reason='stop'")
EMPTY = LLMUnavailable("empty reply: finish_reason='length', completion_tokens=16384")


def test_a_reply_that_was_not_json_is_asked_again():
    c = _Scripted(NOT_JSON, {"verdict": "VETO", "reasons": ["fades, not momentum"]})
    v = _ask(c)
    assert v.verdict == "VETO" and v.attempts == 2 and c.calls == 2


def test_an_empty_reply_is_asked_again():
    c = _Scripted(EMPTY, EMPTY, {"verdict": "PASS"})
    v = _ask(c)
    assert v.verdict == "PASS" and v.attempts == 3


def test_unusable_replies_are_bounded_and_still_not_a_pass():
    c = _Scripted(*([NOT_JSON] * (rv.UNUSABLE_RETRIES + 1)))
    v = _ask(c)
    assert v.verdict == "CANNOT_TELL"
    assert c.calls == rv.UNUSABLE_RETRIES + 1
    assert "did not run" in v.reasons[0]


def test_a_reply_with_no_verdict_is_asked_again_then_refused():
    c = _Scripted({"verdict": "APPROVE"}, {"verdict": "fine"}, {"verdict": "ok"})
    v = _ask(c)
    assert v.verdict == "CANNOT_TELL" and c.calls == rv.UNUSABLE_RETRIES + 1
    # "did not run" is how `seats_failed` and the retry script find it.
    assert "did not run" in v.reasons[0]


def test_a_non_transient_failure_that_is_not_a_reply_is_not_retried():
    c = _Scripted(LLMUnavailable("HTTP 422: budget_exceeded"))
    v = _ask(c)
    assert v.verdict == "CANNOT_TELL" and c.calls == 1


def test_transport_and_unusable_budgets_are_separate():
    c = _Scripted(LLMUnavailable("HTTP 502: proxy"), NOT_JSON,
                  ModelSubstituted("asked x, served y"), {"verdict": "PASS"})
    v = _ask(c)
    assert v.verdict == "PASS" and v.attempts == 4


def test_transport_retries_are_still_bounded():
    c = _Scripted(*([LLMUnavailable("HTTP 502")] * (len(rv.SEAT_BACKOFF_S) + 1)))
    v = _ask(c)
    assert v.verdict == "CANNOT_TELL"
    assert c.calls == len(rv.SEAT_BACKOFF_S) + 1


def test_attempts_reach_the_record():
    cv = rv.CommitteeVerdict(verdict="PASS", auditors=(
        rv.AuditorVerdict(axis="economics", verdict="PASS", attempts=3),))
    assert cv.as_dict()["auditors"][0]["attempts"] == 3


# --------------------------------------------------------------------------- #
# scripts/committee_retry.py
# --------------------------------------------------------------------------- #
def test_failed_seats_are_read_off_old_and_new_records():
    m = _script_mod()
    old = {"verdict": "CANNOT_TELL", "auditors": [
        {"axis": "leakage", "verdict": "PASS", "reasons": []},
        {"axis": "economics", "verdict": "CANNOT_TELL",
         "reasons": ["the auditor did not run: HTTP 502"], "error": "HTTP 502"},
        {"axis": "robustness", "verdict": "CANNOT_TELL",
         "reasons": ["breadth is thin; cannot judge"]},
    ]}
    # A considered CANNOT_TELL is a verdict, not a failure.
    assert m.failed_seats(old) == ["economics"]
    assert set(m.failed_seats({"verdict": "ERROR"})) == set(rv.AXES)


def test_a_seat_the_retry_already_resolved_is_not_asked_again(tmp_path):
    m = _script_mod()
    (tmp_path / "committee-verdict.json").write_text(json.dumps({
        "verdict": "ERROR"}))
    (tmp_path / m.RETRY_FILE).write_text(json.dumps({"seats": {
        "economics": {"verdict": "VETO"},
        "leakage": {"verdict": "CANNOT_TELL", "error": "HTTP 502"}}}))
    left = m.pending(tmp_path, None)
    assert "economics" not in left and "leakage" in left


def test_the_retry_never_writes_over_the_committed_verdict():
    src = (ROOT / "scripts" / "committee_retry.py").read_text()
    assert 'RETRY_FILE = "committee-seat-retry.json"' in src
    assert '"committee-verdict.json").write' not in src


def test_the_source_falls_back_to_the_best_ledger_row(tmp_path):
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import nq_campaign as nc

    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "aa.py").write_text("# best")
    (tmp_path / "sources" / "bb.py").write_text("# worse")
    rows = [
        {"score": {"primary": 0.1}, "strategy_source": "runs/campaigns/x/sources/bb.py"},
        {"score": {"primary": 0.3}, "strategy_source": "runs/campaigns/x/sources/aa.py"},
        {"score": {"primary": 0.9, "rejected": True},
         "strategy_source": "runs/campaigns/x/sources/bb.py"},
        {"score": None, "strategy_source": "runs/campaigns/x/sources/bb.py"},
    ]
    (tmp_path / "ledger.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n")
    assert nc._source_from_ledger(tmp_path) == "# best"
