"""Amendment 7: the synthetic probe. What crosses to the agent, what cannot, and
that nothing here can change what counts toward N.

Three properties carry the weight.

1. **The channel is a whitelist by construction.** `ProbeResult` has fields for
   crashed, traceback, trade count and fill-log digest, and none for a score or
   a P&L. A row from `run.py` carries a score; the projection drops it.
2. **The probe can only ever run on synthetic data.** Both probe profiles name a
   dataset whose `kind` is `synthetic` with `signal_strength: 0`. That is the
   whole reason this is not the check `scripts/agent_check.py` records Task 23
   rejecting: the binary-search attack still works and recovers a number about
   seeded noise.
3. **The probe informs and never gates.** Every path out of `_after_static`
   returns a statically-clean file and the trial runs on it. A crash after the
   repair budget, a probe that cannot run, a repair call that fails, a repair
   that breaks the static check -- each leaves the last good file standing.
   Nothing here can produce a blocked proposal, so N is untouched.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import fields
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.loop import llm, probe  # noqa: E402
from src.core.loop.campaign import CampaignRecord  # noqa: E402
from tests.test_llm_agent import GATE_WORDS  # noqa: E402

PLAIN = REPO_ROOT / "src" / "strategies" / "current.py"

CRASHES = '''"""TRANSIENT test fixture: raises on the first bar."""


class Strategy:
    def setup(self, ctx):
        ctx.subscribe("5m")

    def on_bars(self, ctx):
        raise RuntimeError("deliberate probe crash: marker 7f3a")

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
'''

FITTED = '''
class Strategy:
    def setup(self, ctx): ctx.subscribe("5m")
    def on_bars(self, ctx): pass
    def on_fill(self, ctx): pass
    def on_lifecycle(self, ctx): pass
    def fit(self): self.t = 1.0
    def save_weights(self): return b"1.0"
    def load_weights(self, blob): self.t = float(blob)
'''


def _agent(**kw):
    os.environ.setdefault("LITELLM_BASE_URL", "https://example.invalid/v1")
    os.environ.setdefault("LITELLM_API_KEY", "not-a-key-and-never-sent")
    return llm.LLMAgent(model="test-model", **kw)


def _result(**kw) -> probe.ProbeResult:
    base = dict(ran=True, crashed=False, profile="agent", trade_count=12,
                fill_log_digest="d-candidate", error="", traceback_tail="",
                wall_clock_s=0.1)
    base.update(kw)
    return probe.ProbeResult(**base)


# --------------------------------------------------------------------------- #
# 1. the channel
# --------------------------------------------------------------------------- #
def test_the_result_has_no_field_that_could_carry_a_score():
    names = {f.name for f in fields(probe.ProbeResult)}
    assert names == {"ran", "crashed", "profile", "trade_count",
                     "fill_log_digest", "error", "traceback_tail", "wall_clock_s"}
    for forbidden in ("score", "primary", "pnl", "sortino", "net", "returns",
                      "sessions", "downside"):
        assert not any(forbidden in n for n in names), forbidden


def test_the_projection_drops_the_score_the_row_carries():
    row = {"outcome": "completed", "wall_clock_s": 0.09,
           "fill_log_digest": "abc",
           "score": {"primary": 1.234, "trade_count": 70, "n_sessions": 8,
                     "rejected": True, "reject_reason": "8 < 30 sessions"}}
    res = probe.result_from_row(row, "agent")
    assert res.ran and not res.crashed
    assert res.trade_count == 70 and res.fill_log_digest == "abc"
    assert "1.234" not in json.dumps(res.as_dict())
    assert "primary" not in json.dumps(res.as_dict())


def test_a_crashed_row_projects_to_a_bounded_traceback():
    row = {"outcome": "crashed", "error": "RuntimeError: boom",
           "child_traceback": "x" * 5000 + "\nRuntimeError: boom",
           "fill_log_digest": None, "score": None}
    res = probe.result_from_row(row, "agent")
    assert res.crashed and "boom" in res.error
    assert res.traceback_tail.endswith("RuntimeError: boom")
    assert len(res.traceback_tail) == probe.TRACEBACK_TAIL_CHARS


def test_a_harness_failure_or_an_empty_row_is_not_a_crash():
    assert probe.result_from_row({"outcome": "harness_failed",
                                  "error": "pipe"}, "agent").ran is False
    assert probe.result_from_row({}, "agent").ran is False


# --------------------------------------------------------------------------- #
# 2. synthetic only
# --------------------------------------------------------------------------- #
def test_both_probe_profiles_are_seeded_noise_with_no_signal():
    cfg = yaml.safe_load((REPO_ROOT / "config" / "run.yaml").read_text())
    for name in probe.PROBE_PROFILES.values():
        prof = cfg["profiles"][name]
        ds = cfg["datasets"][prof["dataset"]]
        assert ds["kind"] == "synthetic", (name, prof["dataset"])
        assert float(ds.get("signal_strength", 0.0)) == 0.0, name
        assert "weights_path" not in prof, name
        assert prof["split"] != "holdout"


def test_the_profile_follows_the_proposal_not_the_campaign():
    assert probe.probe_profile_for(PLAIN.read_text()) == "agent"
    assert probe.probe_profile_for(FITTED) == "harness-fitted"


# --------------------------------------------------------------------------- #
# the real thing, once each way -- about a second per run
# --------------------------------------------------------------------------- #
def test_a_running_file_probes_clean_and_the_row_is_a_check_not_a_trial(tmp_path):
    backup = PLAIN.read_text()
    ledger = tmp_path / "probe-ledger.jsonl"
    res = probe.probe_source(backup, ledger=ledger)
    assert PLAIN.read_text() == backup, "current.py was not restored"
    assert res.ran and not res.crashed, res
    assert isinstance(res.trade_count, int) and res.trade_count > 0
    assert res.fill_log_digest
    rows = [json.loads(x) for x in ledger.read_text().splitlines() if x.strip()]
    assert rows[-1]["purpose"] == "check", "a probe row must never count as a trial"
    assert rows[-1]["trigger"] == "human"


def test_a_crashing_file_probes_crashed_with_its_traceback(tmp_path):
    backup = PLAIN.read_text()
    res = probe.probe_source(CRASHES, ledger=tmp_path / "probe-ledger.jsonl")
    assert PLAIN.read_text() == backup
    assert res.ran and res.crashed
    assert "marker 7f3a" in res.error or "marker 7f3a" in res.traceback_tail


# --------------------------------------------------------------------------- #
# 3. informs, never gates
# --------------------------------------------------------------------------- #
class _Fake:
    """A probe and a model, scripted. `table` maps source text -> ProbeResult
    (or an exception to raise); `replies` is what `_one` returns, in order."""

    def __init__(self, table: dict, replies: list[str]):
        self.table, self.replies = table, list(replies)
        self.probed: list[str] = []
        self.prompts: list[tuple[str, str]] = []

    def probe(self, source: str) -> probe.ProbeResult:
        self.probed.append(source)
        out = self.table[source]
        if isinstance(out, Exception):
            raise out
        return out

    def one(self, prompt: str, kind: str) -> str:
        self.prompts.append((kind, prompt))
        return self.replies.pop(0)


def _wired(tmp_path, table, replies, **kw):
    champion = tmp_path / "champion.py"
    champion.write_text("REF")
    fake = _Fake(table, replies)
    agent = _agent(probe_before_trial=True, probe=fake.probe, champion=champion,
                   show_history=False, **kw)
    def one(prompt: str, kind: str) -> str:
        # The real `_one` logs a Call per request; the fake does the same so
        # `drain_log` counts what it would count live.
        agent._pending.append(llm.Call(kind, True, 0.0))
        return fake.one(prompt, kind)
    agent._one = one                                        # type: ignore[method-assign]
    agent._static_ok = lambda src: (True, "")               # type: ignore[method-assign]
    return agent, fake


def test_a_clean_probe_proceeds_and_the_reference_is_probed_once(tmp_path):
    agent, fake = _wired(
        tmp_path,
        {"REF": _result(fill_log_digest="d-ref"), "A": _result(), "B": _result()},
        ["A", "B"])
    assert agent.propose([]) == "A"
    log = agent.drain_log()
    assert log["probe_runs"] == 1 and log["probe_repairs"] == 0
    assert agent.propose([]) == "B"
    assert agent.drain_log()["probe_runs"] == 1
    assert fake.probed.count("REF") == 1, "the reference costs one probe per champion"


def test_a_crash_is_repaired_with_the_traceback_and_never_the_score(tmp_path):
    crashed = _result(crashed=True, error="RuntimeError: marker 7f3a",
                      traceback_tail="  File x\nRuntimeError: marker 7f3a",
                      fill_log_digest=None, trade_count=None)
    agent, fake = _wired(
        tmp_path,
        {"REF": _result(fill_log_digest="d-ref"), "A": crashed, "A2": _result()},
        ["A", "A2"])
    assert agent.propose([]) == "A2"
    kinds = [k for k, _ in fake.prompts]
    assert kinds == ["propose", "probe_repair"]
    # What goes on the wire starts at `## SYSTEM`; the template's own comment
    # above it names the forbidden words and is never sent (see test_llm_agent).
    repair_prompt = fake.prompts[1][1].split("## SYSTEM", 1)[-1].lower()
    assert "marker 7f3a" in repair_prompt and "synthetic" in repair_prompt
    found = [w for w in GATE_WORDS if w in repair_prompt]
    assert not found, found
    # The template itself says "score" (the objective the agent is shown); the
    # probe's own text may say it once, in the sentence saying it reports none.
    repair_text = llm.LLMAgent._probe_repair_text(crashed, False, "A").lower()
    assert "reports no score" in repair_text
    for leaked in ("p&l", "pnl", "sortino", "profit"):
        assert leaked not in repair_text, leaked
    log = agent.drain_log()
    assert log["probe_repairs"] == 1 and log["probe_runs"] == 2


def test_an_edit_identical_to_the_reference_is_repaired(tmp_path):
    agent, fake = _wired(
        tmp_path,
        {"REF": _result(fill_log_digest="same"),
         "A": _result(fill_log_digest="same"),
         "A2": _result(fill_log_digest="different")},
        ["A", "A2"])
    assert agent.propose([]) == "A2"
    assert "byte-identical" in fake.prompts[1][1].lower()


def test_exhausting_the_repair_budget_proceeds_with_the_last_file(tmp_path):
    crashed = _result(crashed=True, error="still broken", fill_log_digest=None)
    agent, fake = _wired(
        tmp_path,
        {"REF": _result(fill_log_digest="d-ref"),
         "A": crashed, "B": crashed, "C": crashed},
        ["A", "B", "C"], max_probe_repairs=2)
    out = agent.propose([])
    assert out == "C", "the trial runs on the last statically-clean file"
    assert "agent_blocked" not in out, "the probe must never produce a blocked proposal"
    log = agent.drain_log()
    assert log["probe_runs"] == 3 and log["probe_repairs"] == 2
    assert log["blocked"] is False


def test_a_reference_that_crashes_on_the_probe_disarms_the_crash_repair(tmp_path):
    """viperq_amt crashes on the 20-session synthetic fit window. A campaign
    seeded with it must not spend two repair calls per trial on that fact."""
    crashed = _result(crashed=True, error="fit() reached with 0 usable rows",
                      fill_log_digest=None)
    agent, fake = _wired(
        tmp_path, {"REF": crashed, "A": crashed}, ["A"])
    assert agent.propose([]) == "A"
    assert [k for k, _ in fake.prompts] == ["propose"]


def test_a_probe_that_cannot_run_costs_the_proposal_nothing(tmp_path):
    agent, fake = _wired(
        tmp_path,
        {"REF": _result(fill_log_digest="d-ref"), "A": OSError("no run.py")},
        ["A"])
    assert agent.propose([]) == "A"
    log = agent.drain_log()
    assert log["probe_runs"] == 1 and log["calls"][-1]["ok"] is False
    assert log["blocked"] is False


def test_a_repair_that_breaks_the_static_check_is_discarded(tmp_path):
    crashed = _result(crashed=True, error="boom", fill_log_digest=None)
    agent, fake = _wired(
        tmp_path, {"REF": _result(fill_log_digest="d-ref"), "A": crashed},
        ["A", "not even python"])
    calls = {"n": 0}

    def static_ok(src):
        calls["n"] += 1
        return (src != "not even python"), "E001 no Strategy class"
    agent._static_ok = static_ok                            # type: ignore[method-assign]
    assert agent.propose([]) == "A"
    assert "not even python" not in fake.probed


def test_the_flag_and_the_callable_are_both_required(tmp_path):
    fake = _Fake({}, [])
    agent = _agent(probe_before_trial=False, probe=fake.probe, show_history=False)
    agent._one = lambda prompt, kind: "A"                   # type: ignore[method-assign]
    agent._static_ok = lambda src: (True, "")               # type: ignore[method-assign]
    assert agent.propose([]) == "A" and fake.probed == []
    assert agent.config()["probe_before_trial"] is False
    agent2 = _agent(probe_before_trial=True, probe=None, show_history=False)
    agent2._one = lambda prompt, kind: "A"                  # type: ignore[method-assign]
    agent2._static_ok = lambda src: (True, "")              # type: ignore[method-assign]
    assert agent2.propose([]) == "A"
    assert agent2.config()["probe_before_trial"] is True


def test_the_repair_texts_never_mention_the_gate():
    crashed = _result(crashed=True, error="RuntimeError: x", traceback_tail="tb",
                      fill_log_digest=None)
    for res, same in ((crashed, False), (_result(), True)):
        text = llm.LLMAgent._probe_repair_text(res, same, "SRC").lower()
        assert not [w for w in GATE_WORDS if w in text]
        assert "validation" not in text and "holdout" not in text


# --------------------------------------------------------------------------- #
# the record and the script
# --------------------------------------------------------------------------- #
def test_adding_the_field_did_not_move_any_existing_records_digest():
    a = CampaignRecord(campaign_id="x", profile="p", budget=1, q=0.05)
    b = CampaignRecord(campaign_id="x", profile="p", budget=1, q=0.05,
                       probe_before_trial=None)
    assert a.digest() == b.digest()
    assert CampaignRecord(campaign_id="x", profile="p", budget=1, q=0.05,
                          probe_before_trial=False).digest() != a.digest()


def test_the_config_default_is_on_since_amendment_7_and_the_flag_is_tri_state():
    """Signed 2026-10-01, and the default flipped with it.

    The `false` default existed so no campaign could run an unsigned mechanism
    by accident; once signed the opposite risk takes over, which is forgetting
    the flag on a campaign that should have had it.

    And the flag itself was broken. `--probe` was `store_true, default=False`
    while registration applied it whenever it was "not None" -- which it never
    was -- so the flag silently overrode the config to False on EVERY
    registration and the config default could never take effect. A tri-state
    now: omit for the config, `--probe`/`--no-probe` to decide per campaign.
    """
    cfg = yaml.safe_load((REPO_ROOT / "config" / "run.yaml").read_text())
    assert cfg["campaign"]["probe_before_trial"] is True
    source = (REPO_ROOT / "scripts" / "nq_campaign.py").read_text()
    assert "action=argparse.BooleanOptionalAction" in source
    assert "default=None" in source
    assert "if probe is not None:" in source, (
        "registration must fall through to the config when no flag was given")
    assert 'probe_before_trial=bool(cc.get("probe_before_trial"))' in source
    assert 'agent_cfg["probe_before_trial"]' in source
    # the run stage reads the record, never the config
    run_stage = source[source.index("def stage_run"):source.index("def stage_nulls")]
    assert "record.probe_before_trial" in run_stage
    assert "probe_source" in run_stage and "probe-ledger.jsonl" in run_stage
    assert 'cfg()["campaign"]' not in run_stage
