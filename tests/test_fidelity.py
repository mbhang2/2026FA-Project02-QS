"""What the question fixes, checked: `src/core/loop/fidelity.py` and its wiring.

No network and no market data. Fills are built by hand; the agent's model call is
a fake that replays a script.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.core.loop import fidelity as F
from src.core.loop import llm
from src.core.loop.campaign import CampaignRecord, CampaignResult, summarise

ROOT = Path(__file__).resolve().parents[1]
CT = ZoneInfo("America/Chicago")

HYP = """# Does aggressive flow continue?

## What is NOT yours to choose

- Signal measured over 5 minutes. Positions are held at least 5 minutes.
- It must condition on a level built from the volume profile.
- Entries only in the first hour of regular trading.
- No position is carried into the next session.
- The strategy is long only.
- One contract at a time.
- Flat by 15:00 CT.
- It never fits a model.
"""


def _yaml(tmp_path: Path, constraints: list[dict], **top) -> Path:
    import yaml

    p = tmp_path / "flow.constraints.yaml"
    p.write_text(yaml.safe_dump({"schema": F.SCHEMA, "hypothesis": "flow.md",
                                 **top, "constraints": constraints}))
    return p


ALL = [
    {"id": "hold-5m", "says": "Positions are held at least 5 minutes.",
     "check": "hold_seconds", "min": 300},
    {"id": "profile", "says": "It must condition on a level built from the volume profile.",
     "check": "code_uses", "any_of": ["volume_profile", "SessionProfile"]},
    {"id": "first-hour", "says": "Entries only in the first hour of regular trading.",
     "check": "entry_window", "start": "08:30", "end": "09:30"},
    {"id": "one-session", "says": "No position is carried into the next session.",
     "check": "no_overnight"},
    {"id": "long", "says": "The strategy is long only.", "check": "side", "only": "long"},
    {"id": "one-lot", "says": "One contract at a time.", "check": "max_position", "value": 1},
    {"id": "flat-3pm", "says": "Flat by 15:00 CT.", "check": "flat_by", "time": "15:00"},
    {"id": "no-fit", "says": "It never fits a model.", "check": "code_avoids",
     "none_of": ["fit", "sklearn"]},
]


def _cons(tmp_path, items=None, **top) -> F.Constraints:
    return F.load(_yaml(tmp_path, items or ALL, **top), HYP)


# --------------------------------------------------------------------------- #
# loading: a constraint the question did not state is refused
# --------------------------------------------------------------------------- #
def test_every_kind_loads_and_the_file_is_digested(tmp_path):
    c = _cons(tmp_path)
    assert {x.check for x in c.items} == set(F.KINDS)
    assert len(c.digest) == 64 and c.clock == "America/Chicago"
    assert {x.id for x in c.code} == {"profile", "no-fit"}


def test_a_quote_that_is_not_in_the_hypothesis_is_refused(tmp_path):
    bad = [{"id": "hold", "says": "Positions are held at least 10 minutes.",
            "check": "hold_seconds", "min": 600}]
    with pytest.raises(F.ConstraintsRejected, match="verbatim"):
        _cons(tmp_path, bad)


def test_the_quote_is_whitespace_and_case_insensitive(tmp_path):
    ok = [{"id": "hold", "says": "positions  are held\nat least 5 MINUTES.",
           "check": "hold_seconds", "min": 300}]
    assert _cons(tmp_path, ok).items[0].id == "hold"


@pytest.mark.parametrize("item, match", [
    ({"id": "x", "says": "Flat by 15:00 CT.", "check": "flat_at"}, "unknown check"),
    ({"id": "x", "says": "Flat by 15:00 CT.", "check": "flat_by", "tme": "15:00"}, "needs"),
    ({"id": "x", "says": "Flat by 15:00 CT.", "check": "flat_by", "time": "15:00",
      "minn": 1}, "does not take"),
    ({"id": "X Y", "says": "Flat by 15:00 CT.", "check": "flat_by", "time": "15:00"}, "kebab"),
    ({"id": "x", "says": "Flat by 15:00 CT.", "check": "hold_seconds"}, "min or max"),
    ({"id": "x", "says": "Flat by 15:00 CT.", "check": "side", "only": "both"}, "long or short"),
    ({"id": "x", "says": "Flat by 15:00 CT.", "check": "entry_window",
      "start": "10:00", "end": "09:00"}, "before"),
    ({"id": "x", "says": "Flat by 15:00 CT.", "check": "flat_by", "time": "15:00",
      "share": 0}, "share"),
    ({"id": "x", "says": "Flat by 15:00 CT.", "check": "code_uses",
      "any_of": ["not an identifier"]}, "identifiers"),
])
def test_malformed_constraints_are_refused_not_ignored(tmp_path, item, match):
    with pytest.raises(F.ConstraintsRejected, match=match):
        _cons(tmp_path, [item])


def test_duplicate_ids_and_empty_files_are_refused(tmp_path):
    with pytest.raises(F.ConstraintsRejected, match="duplicate"):
        _cons(tmp_path, [ALL[0], ALL[0]])
    with pytest.raises(F.ConstraintsRejected, match="no constraints"):
        F.load(_yaml(tmp_path, []), HYP)


def test_the_sidecar_sits_beside_its_hypothesis():
    assert F.sidecar_for(Path("config/hypotheses/flow-continuation-5m.md")) == \
        Path("config/hypotheses/flow-continuation-5m.constraints.yaml")


# --------------------------------------------------------------------------- #
# code: what runs, not what is said
# --------------------------------------------------------------------------- #
USES = '''
from src.core.features.volume_profile import SessionProfile
class Strategy:
    def on_bars(self, ctx):
        p = SessionProfile()
'''
SAYS = '''
"""This strategy conditions on the volume_profile and its SessionProfile."""
from src.core.features.volume_profile import SessionProfile   # imported, never used
class Strategy:
    def on_bars(self, ctx):
        note = "volume_profile"   # a string is not use
        # volume_profile
'''


def test_using_the_feature_satisfies_code_uses(tmp_path):
    c = _cons(tmp_path, [ALL[1]])
    assert F.code_violations(USES, c) == []


def test_docstrings_comments_strings_and_bare_imports_are_not_use(tmp_path):
    c = _cons(tmp_path, [ALL[1]])
    v = F.code_violations(SAYS, c)
    assert [x.id for x in v] == ["profile"]


def test_code_avoids_sees_imports_and_calls(tmp_path):
    c = _cons(tmp_path, [ALL[7]])
    assert [x.id for x in F.code_violations("import sklearn\n", c)] == ["no-fit"]
    assert [x.id for x in F.code_violations("class S:\n    def fit(self): pass\n"
                                            "S().fit()\n", c)] == ["no-fit"]
    assert F.code_violations("x = 'fit'\n", c) == []


def test_a_syntax_error_is_the_static_checks_business(tmp_path):
    assert F.code_violations("def (:\n", _cons(tmp_path, [ALL[1]])) == []


# --------------------------------------------------------------------------- #
# behaviour: read off the fills
# --------------------------------------------------------------------------- #
def _ns(day: int, hh: int, mm: int, ss: int = 0) -> int:
    t = dt.datetime(2024, 7, day, hh, mm, ss, tzinfo=CT)
    return int(t.timestamp() * 1e9)


def _fill(seq, ts, session, side, size=1):
    return [seq, ts, session, seq, side, size, 20000.0, "taker", ""]


def _turn(seq, day, h0, m0, h1, m1, side="buy", size=1, exit_day=None, s1=None):
    other = "sell" if side == "buy" else "buy"
    s0 = 700000 + day
    return [_fill(seq, _ns(day, h0, m0), s0, side, size),
            _fill(seq + 1, _ns(exit_day or day, h1, m1), s1 or s0, other, size)]


def _violations(tmp_path, rows, *ids):
    keep = [c for c in ALL if c["id"] in ids]
    return [v.id for v in F.behaviour_violations(rows, _cons(tmp_path, keep))]


def test_round_turns_are_flat_to_flat_and_a_flip_does_not_end_one():
    rows = [_fill(1, 10, 5, "buy"), _fill(2, 20, 5, "sell", 2),
            _fill(3, 30, 5, "buy"), _fill(4, 40, 5, "buy")]
    turns, worst = F.round_turns(rows)
    assert len(turns) == 1 and turns[0].quantity == 1
    assert turns[0].exit_ts == 30 and worst == 1
    # the last fill opens a position that never closes: not a round turn
    assert F.round_turns(rows)[0][0].max_abs == 1


def test_hold_time(tmp_path):
    long_ = _turn(1, 8, 8, 40, 8, 50)                    # 10 min
    short = _turn(3, 9, 8, 40, 8, 41)                    # 1 min: one of two is 50%
    assert _violations(tmp_path, long_, "hold-5m") == []
    assert _violations(tmp_path, long_ + short, "hold-5m") == ["hold-5m"]


def test_hold_time_share_admits_a_minority(tmp_path):
    rows = sum((_turn(2 * k, 8 + k, 8, 40, 8, 50) for k in range(9)), [])
    rows += _turn(100, 20, 8, 40, 8, 41)
    item = dict(ALL[0], share=0.9)
    assert F.behaviour_violations(rows, _cons(tmp_path, [item])) == []
    # the default is 95%, so one in ten is too many
    assert F.behaviour_violations(rows, _cons(tmp_path, [ALL[0]])) != []
    strict = dict(ALL[0], share=1)
    rows20 = sum((_turn(2 * k, 1 + k, 8, 40, 8, 50) for k in range(20)), [])
    rows20 += _turn(100, 25, 8, 40, 8, 41)               # 1 in 21: inside 95%
    assert F.behaviour_violations(rows20, _cons(tmp_path, [ALL[0]])) == []
    assert F.behaviour_violations(rows20, _cons(tmp_path, [strict])) != []


def test_entry_window_is_on_the_exchange_clock_across_dst(tmp_path):
    # 08:45 CT in July (CDT) and in December (CST) are both inside 08:30-09:30.
    summer = _turn(1, 8, 8, 45, 9, 0)
    winter_ts = int(dt.datetime(2024, 12, 9, 8, 45, tzinfo=CT).timestamp() * 1e9)
    winter = [_fill(5, winter_ts, 1, "buy"), _fill(6, winter_ts + 60 * 10**9, 1, "sell")]
    assert _violations(tmp_path, summer + winter, "first-hour") == []
    late = _turn(9, 10, 10, 15, 10, 30)
    assert _violations(tmp_path, summer + late, "first-hour") == ["first-hour"]


def test_carried_across_a_session_is_overnight(tmp_path):
    same = _turn(1, 8, 9, 0, 14, 0)
    carried = _turn(3, 9, 15, 0, 9, 0, exit_day=10, s1=700010)
    assert _violations(tmp_path, same, "one-session") == []
    assert _violations(tmp_path, same + carried, "one-session") == ["one-session"]


def test_flat_by_and_the_evening_segment(tmp_path):
    by_two = _turn(1, 8, 9, 0, 14, 0)
    evening = _turn(3, 8, 17, 30, 18, 0)                 # the session's evening part
    late = _turn(5, 9, 9, 0, 15, 30)
    assert _violations(tmp_path, by_two + evening, "flat-3pm") == []
    assert _violations(tmp_path, by_two + late, "flat-3pm") == ["flat-3pm"]


def test_side_and_max_position(tmp_path):
    longs = _turn(1, 8, 9, 0, 9, 30)
    shorts = _turn(3, 9, 9, 0, 9, 30, side="sell")
    two = _turn(5, 10, 9, 0, 9, 30, size=2)
    assert _violations(tmp_path, longs, "long", "one-lot") == []
    assert _violations(tmp_path, longs + shorts, "long") == ["long"]
    assert _violations(tmp_path, longs + two, "one-lot") == ["one-lot"]


def test_no_trades_contradicts_nothing(tmp_path):
    assert F.behaviour_violations([], _cons(tmp_path)) == []


# --------------------------------------------------------------------------- #
# the campaign's record
# --------------------------------------------------------------------------- #
def test_check_trial_reads_the_stored_fill_log(tmp_path):
    fills = tmp_path / "fills" / "abc.jsonl.gz"
    fills.parent.mkdir()
    with gzip.open(fills, "wt") as fh:
        for r in _turn(1, 8, 8, 40, 8, 41):
            fh.write(json.dumps(r) + "\n")
    row = {"fill_log_source": "fills/abc.jsonl.gz"}
    code = [{"id": "profile", "check": "code_uses", "says": "s", "detail": "d"}]
    got = F.check_trial(row, _cons(tmp_path, [ALL[0], ALL[1]]), root=tmp_path, code=code)
    assert [v["id"] for v in got] == ["profile", "hold-5m"]
    # no stored log -> nothing to contradict
    assert F.check_trial({"fill_log_source": None}, _cons(tmp_path, [ALL[0]]),
                         root=tmp_path) == []


def test_every_trial_gets_a_line_and_only_enforce_excludes(tmp_path):
    F.append_ledger(tmp_path, 0, [], "enforce")
    F.append_ledger(tmp_path, 1, [{"id": "hold-5m", "detail": "x"}], "enforce")
    assert set(F.read_ledger(tmp_path)) == {0, 1}
    enforce = {"constraints": {"policy": "enforce"}}
    record = {"constraints": {"policy": "record"}}
    assert F.excluded_trials(tmp_path, enforce) == {1}
    assert F.excluded_trials(tmp_path, record) == set()
    assert F.excluded_trials(tmp_path, {}) == set()
    assert F.excluded_trials(tmp_path, None) == set()


def _score(p: float) -> dict:
    return {"primary": p, "sharpe": p, "skew": 0.0, "kurtosis": 0.0,
            "n_sessions": 100, "net_pnl": 1.0, "trade_count": 50,
            "rejected": False, "reject_reason": "", "metric": "sortino"}


def test_the_gate_excludes_off_question_from_the_observed_side_only():
    rows = [{"outcome": "completed", "score": _score(p), "campaign_index": i}
            for i, p in enumerate((0.1, 0.9, 0.2))]
    result = CampaignResult(
        record=CampaignRecord(campaign_id="x", profile="campaign", budget=3, q=0.05),
        record_digest="d", rows=rows)
    nulls = [[0.0, 0.05, 0.1, 0.15] * 25 for _ in rows]
    full = summarise(result, dataset="synthetic", null_samples=nulls)
    cut = summarise(result, dataset="synthetic", null_samples=nulls, exclude={1})
    assert full["gate"]["observed_best"] == 0.9
    assert cut["gate"]["observed_best"] == 0.2
    assert cut["gate"]["excluded_off_question"] == [1]
    # N and the null are the same: the bar did not move, only the observed side.
    assert cut["n_trials"] == full["n_trials"] == 3
    assert cut["gate"]["null_max_mean"] == full["gate"]["null_max_mean"]
    assert cut["gate"]["p"] >= full["gate"]["p"]


# --------------------------------------------------------------------------- #
# the proposer: repair, then record or block
# --------------------------------------------------------------------------- #
GOOD = '''
from src.core.features.volume_profile import SessionProfile
class Strategy:
    def setup(self, ctx): pass
    def on_bars(self, ctx):
        self.p = SessionProfile()
    def on_fill(self, ctx): pass
    def on_lifecycle(self, ctx): pass
'''
BAD = '''
class Strategy:
    """Conditions on the volume profile."""
    def setup(self, ctx): pass
    def on_bars(self, ctx): pass
    def on_fill(self, ctx): pass
    def on_lifecycle(self, ctx): pass
'''


def _agent(tmp_path, replies, policy="record"):
    os.environ["LITELLM_BASE_URL"] = "https://example.invalid/v1"
    os.environ["LITELLM_API_KEY"] = "not-a-key-and-never-sent"
    agent = llm.LLMAgent(model="test-model", show_history=False,
                         constraints=_cons(tmp_path, [ALL[1]]),
                         constraint_policy=policy)
    script = list(replies)
    prompts = []

    def one(prompt: str, kind: str) -> str:
        agent._pending.append(llm.Call(kind, True, 0.0))
        prompts.append((kind, prompt))
        return script.pop(0)
    agent._one = one                                        # type: ignore[method-assign]
    agent._static_ok = lambda src: (True, "")               # type: ignore[method-assign]
    return agent, prompts


def test_a_clean_proposal_costs_no_repair(tmp_path):
    agent, prompts = _agent(tmp_path, [GOOD])
    assert agent.propose([]) == GOOD
    log = agent.drain_log()
    assert log["fidelity_repairs"] == 0 and log["off_question_code"] == []


def test_a_violation_is_repaired_with_the_quote_and_never_a_score(tmp_path):
    agent, prompts = _agent(tmp_path, [BAD, GOOD])
    assert agent.propose([]) == GOOD
    kind, prompt = prompts[1]
    assert kind == "fidelity_repair"
    assert "It must condition on a level built from the volume profile." in prompt
    assert "executes nothing" in prompt and "score" in prompt   # "reports no score"
    assert agent.drain_log()["fidelity_repairs"] == 1


def test_unrepaired_under_record_goes_to_trial_and_is_logged(tmp_path):
    agent, _ = _agent(tmp_path, [BAD] * (F.MAX_REPAIRS + 1))
    assert agent.propose([]) == BAD
    log = agent.drain_log()
    assert log["fidelity_repairs"] == F.MAX_REPAIRS
    assert [v["id"] for v in log["off_question_code"]] == ["profile"]
    assert not log["blocked"]


def test_unrepaired_under_enforce_is_a_blocked_trial(tmp_path):
    agent, _ = _agent(tmp_path, [BAD] * (F.MAX_REPAIRS + 1), policy="enforce")
    out = agent.propose([])
    assert "agent_blocked: off_question_unrepaired" in out
    assert agent.drain_log()["blocked"]


def test_the_record_carries_the_constraints_by_relative_path(tmp_path):
    agent, _ = _agent(tmp_path, [])
    cfg = agent.config()["constraints"]
    assert cfg["policy"] == "record" and len(cfg["digest"]) == 64
    agent.constraints = None
    assert agent.config()["constraints"] is None


def test_the_agent_sees_an_off_question_trial_in_its_history(tmp_path):
    agent, _ = _agent(tmp_path, [])
    agent.show_history = True
    row = {"outcome": "completed", "score": _score(0.3),
           "off_question": [{"id": "hold-5m", "detail": "3 of 40 round turns held "
                             "at least 300 s"}]}
    text = agent._history_block([row])
    assert "OFF-QUESTION [hold-5m] 3 of 40 round turns" in text


# --------------------------------------------------------------------------- #
# the committed sidecars, if any, all load against their hypotheses
# --------------------------------------------------------------------------- #
def test_every_committed_constraints_file_loads():
    for side in sorted((ROOT / "config" / "hypotheses").glob("*.constraints.yaml")):
        hyp = side.with_name(side.name.replace(".constraints.yaml", ".md"))
        assert hyp.is_file(), f"{side.name} has no hypothesis beside it"
        F.load(side, hyp.read_text())


# --------------------------------------------------------------------------- #
# the shadow reviewer's model-free half
# --------------------------------------------------------------------------- #
def _judged(tmp_path):
    return _cons(tmp_path, [ALL[0]], judged=[
        {"id": "long-only", "says": "The strategy is long only."},
        {"id": "one-lot", "says": "One contract at a time."}])


def test_judged_requirements_are_grounded_too(tmp_path):
    assert [j.id for j in _judged(tmp_path).judged] == ["long-only", "one-lot"]
    with pytest.raises(F.ConstraintsRejected, match="verbatim"):
        _cons(tmp_path, [ALL[0]], judged=[{"id": "x", "says": "Never short."}])
    with pytest.raises(F.ConstraintsRejected, match="duplicate"):
        _cons(tmp_path, [ALL[0]], judged=[{"id": "hold-5m", "says": "One contract at a time."}])
    with pytest.raises(F.ConstraintsRejected, match="exactly"):
        _cons(tmp_path, [ALL[0]], judged=[{"id": "x", "says": "One contract at a time.",
                                           "check": "side"}])
    # a file with only judged requirements is allowed
    only = F.load(_yaml(tmp_path, [], judged=[{"id": "x", "says": "One contract at a time."}]), HYP)
    assert not only.items and len(only.judged) == 1


def test_the_reviewer_reads_no_comments_or_docstrings():
    src = '''"""Module: follows the flow."""
class Strategy:
    """Never fades."""
    def on_bars(self, ctx):
        """Docstring only."""
        # it continues the move
        ctx.sell()  # fade
'''
    out = F.strip_for_review(src)
    assert "follows" not in out and "fades" not in out and "fade" not in out
    assert "continues" not in out and "Docstring" not in out
    assert "ctx.sell()" in out


def test_a_violation_must_quote_the_code(tmp_path):
    cons = _judged(tmp_path)
    stripped = F.strip_for_review("class S:\n    def on_bars(self, ctx):\n"
                                  "        ctx.sell(size=1)\n")
    reply = {"constraints": [
        {"id": "long-only", "verdict": "VIOLATED", "evidence": "ctx.sell(size=1)",
         "why": "opens short"},
        {"id": "one-lot", "verdict": "VIOLATED", "evidence": "ctx.buy(size=3)",
         "why": "invented"}]}
    got = {v["id"]: v["verdict"] for v in F.parse_judgement(reply, cons, stripped)}
    assert got == {"long-only": "VIOLATED", "one-lot": "CANNOT_TELL"}


def test_a_missing_or_unknown_verdict_is_cannot_tell(tmp_path):
    cons = _judged(tmp_path)
    got = F.parse_judgement({"constraints": [{"id": "long-only", "verdict": "OK"}]},
                            cons, "x = 1")
    assert [v["verdict"] for v in got] == ["CANNOT_TELL", "CANNOT_TELL"]


def test_the_shadow_ledger_is_not_the_one_the_gate_reads(tmp_path):
    F.append_judgement(tmp_path, 0, {"ran": True, "violated": ["long-only"]})
    assert F.read_ledger(tmp_path) == {}
    assert F.excluded_trials(tmp_path, {"constraints": {"policy": "enforce"}}) == set()


def test_nothing_reads_the_shadow_verdicts_to_decide():
    src = (ROOT / "scripts" / "nq_campaign.py").read_text()
    llm_src = (ROOT / "src" / "core" / "loop" / "llm.py").read_text()
    camp = (ROOT / "src" / "core" / "loop" / "campaign.py").read_text()
    summ = (ROOT / "scripts" / "campaign_summary.py").read_text()
    for text in (llm_src, camp, summ):
        assert "fidelity-judge" not in text and "JUDGE_LEDGER" not in text
    # the driver writes it and reads nothing back
    assert "append_judgement" in src and "read_judgement" not in src
