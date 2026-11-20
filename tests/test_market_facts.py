"""Every agent is told the market its campaign trades, and NQ is told what it always was.

The review seats, the question writer, the refiner and the proposer's run facts
each had NQ written into them. A crypto campaign must not be described as NQ,
and an NQ campaign's prompts must not move.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from src.core.loop.market import facts, market_for_profile, render

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ("strategy-review", "hypothesis-review", "hypothesis-refine", "question-distil")


def _text(name: str) -> str:
    return (ROOT / "config" / "prompts" / f"{name}.md").read_text()


@pytest.mark.parametrize("name", PROMPTS)
def test_every_prompt_renders_for_both_markets_with_nothing_left_unfilled(name):
    for market in ("nq", "crypto-BTCUSDT", "crypto-ETHUSDT"):
        out = render(_text(name), market)
        assert "{market_" not in out


@pytest.mark.parametrize("name", PROMPTS)
def test_a_crypto_prompt_does_not_describe_nq(name):
    out = render(_text(name), "crypto-BTCUSDT")
    assert "NQ" not in out and "CME" not in out and "17:00 CT" not in out
    if "{market_name}" in _text(name) or "{market_data}" in _text(name) or "{market_have}" in _text(name):
        assert "BTCUSDT" in out


# The last commit before the markers went in. Its prompts are what NQ was told.
BEFORE_MARKERS = "c896fe3"


@pytest.mark.parametrize("name", ("hypothesis-review", "hypothesis-refine", "question-distil"))
def test_nq_prompts_render_exactly_as_they_were_written(name):
    """The three whose NQ text was moved verbatim into market.py: byte-identical."""
    before = subprocess.run(["git", "show", f"{BEFORE_MARKERS}:config/prompts/{name}.md"],
                            cwd=ROOT, capture_output=True, text=True)
    if before.returncode != 0:
        pytest.skip(f"{BEFORE_MARKERS} is not in this checkout's history")
    assert render(_text(name), "nq") == before.stdout


def test_an_unknown_marker_is_an_error_not_a_blank():
    with pytest.raises(ValueError, match="unfilled market marker"):
        render("trade {market_venue} now", "nq")


def test_the_market_comes_from_the_profiles_dataset():
    cfg = yaml.safe_load((ROOT / "config" / "run.yaml").read_text())
    assert market_for_profile("nq-train", cfg) == "nq"
    assert market_for_profile("crypto-btc-train", cfg) == "crypto-BTCUSDT"
    assert market_for_profile(None, cfg) == "nq"
    assert facts("crypto-ETHUSDT")["data"].startswith("Binance ETHUSDT")


def _campaign_module():
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("nq_campaign_for_test", ROOT / "scripts" / "nq_campaign.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_proposer_is_not_told_a_crypto_run_is_nq():
    m = _campaign_module()
    what, cost = m._dataset_facts(m.cfg()["profiles"]["crypto-btc-train"])
    assert "Binance BTCUSDT" in what and "UTC day" in what
    assert "NQ" not in what + cost and "$20 a point" not in cost
    assert "5 basis points" in cost and "funding" in cost and "0.001 BTC" in cost
    what_nq, cost_nq = m._dataset_facts(m.cfg()["profiles"]["nq-train"])
    assert what_nq == "real NQ futures trades" and "$20 a point" in cost_nq


def test_crypto_campaigns_probe_on_crypto_shaped_synthetic_data():
    from src.core.data.synthetic import SyntheticDataset, SyntheticSpec
    from src.core.loop import perturb, probe
    from src.core.types import Split

    plain = "class S:\n    def on_bars(self, ctx):\n        pass\n"
    assert probe.probe_profile_for(plain, "nq") == "agent"
    assert probe.probe_profile_for(plain, "crypto-BTCUSDT") == "agent-crypto"
    assert perturb.PAIRS["crypto"] == ("probe-crypto-shape", "probe-crypto-shape-flipped")

    cfg = yaml.safe_load((ROOT / "config" / "run.yaml").read_text())
    spec = dict(cfg["datasets"]["synthetic-crypto-shape"])
    spec.pop("kind")
    ds = SyntheticDataset(SyntheticSpec(**spec, seed=1))
    sessions = sorted({t.session for t in ds.ticks(Split.VALIDATION)})
    import datetime as dt
    opens = [dt.datetime.fromtimestamp(ds.session_bounds(s)[0] / 1e9, dt.timezone.utc) for s in sessions]
    assert all(o.hour == 0 and o.minute == 0 for o in opens)
    assert {o.weekday() for o in opens} == set(range(7)) or len(opens) < 7
    assert all((b - a).days == 1 for a, b in zip(opens, opens[1:]))


def test_new_synthetic_options_leave_every_existing_dataset_hashing_as_before():
    from src.core.data.synthetic import SyntheticSpec

    d = SyntheticSpec().as_dict()
    assert "session_open_utc" not in d and "size_scale" not in d
