"""The per-instrument cost file, and slippage as a cost. Synthetic data only."""

from __future__ import annotations

import copy
import pathlib

import pytest
import yaml

import run as runner
from src.core.costs import (CostConfigError, apply_costs, load_registry, row_cost_terms,
                            terms_for)
from src.core.data.synthetic import SyntheticDataset, SyntheticSpec
from src.core.engine import Engine
from src.core.engine.reconstruct import fill_commission, reconstruct
from src.core.scoring.calibration.reference import ReferenceStrategy
from src.core.scoring.randomization import round_turn as rt
from src.core.types import Split

REPO = pathlib.Path(__file__).resolve().parent.parent
FEE = {"model": "per_contract", "maker": 0.1, "taker": 0.1}
NONE = {"model": "per_contract", "rate": 0.0}


def _ds() -> SyntheticDataset:
    return SyntheticDataset(SyntheticSpec(n_sessions=40, ticks_per_session=120, vol_per_tick=0.2,
                                          seed=5, uniform=True))


def _run(strategy=None, **kw) -> Engine:
    engine = Engine.for_test(_ds(), strategy or ReferenceStrategy(), split=Split.TRAIN,
                             warmup_sessions=1, seed=1, commission_per_contract=0.1, **kw)
    engine.run()
    return engine


class RestingBids:
    def setup(self, ctx):
        ctx.subscribe("5m", emit_empty=True)

    def on_bars(self, ctx):
        if not ctx.actionable or ctx.working_order_ids:
            return
        last = ctx.history("5m")[-1]
        if ctx.position:
            ctx.sell(ctx.position, type="limit", limit=last.close + 0.2, tif="GTC")
        else:
            ctx.buy(1, type="limit", limit=last.close - 0.2, tif="GTC")

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass


def test_the_registry_keeps_every_existing_rate():
    registry = load_registry()
    assert registry["NQ"] == {"commission_per_contract": 0.10, "commission_bps": 0.0,
                              "maker_commission_per_contract": 0.10,
                              "maker_commission_bps": 0.0,
                              "slippage_per_contract": 0.0, "slippage_bps": 0.0}
    for symbol in ("BTCUSDT", "ETHUSDT"):
        assert registry[symbol] == {"commission_per_contract": 0.0, "commission_bps": 5.0,
                                    "maker_commission_per_contract": 0.0,
                                    "maker_commission_bps": 5.0,
                                    "slippage_per_contract": 0.0, "slippage_bps": 0.0}


def test_real_profiles_take_their_rates_from_the_file():
    profiles = runner.load_config()["profiles"]
    raw = yaml.safe_load((REPO / "config" / "run.yaml").read_text())["profiles"]
    for name in ("nq-train", "nq-validation", "nq-holdout", "nq-train-fitted",
                 "nq-validation-fitted", "nq-holdout-fitted"):
        assert raw[name]["costs"] == "NQ" and "commission_per_contract" not in raw[name]
        assert profiles[name]["commission_per_contract"] == 0.10
    for name in ("crypto-btc-train", "crypto-btc-validation"):
        assert raw[name]["costs"] == "BTCUSDT" and "commission_bps" not in raw[name]
        assert profiles[name]["commission_bps"] == 5.0
        assert profiles[name]["commission_per_contract"] == 0.0


@pytest.mark.parametrize("entry", [
    {"fee": FEE},
    {"fee": FEE, "slippage": NONE, "typo": 1},
    {"fee": {"model": "per_lot", "maker": 1.0, "taker": 1.0}, "slippage": NONE},
    {"fee": {"model": "per_contract", "rate": 0.1}, "slippage": NONE},
    {"fee": {"model": "per_contract", "maker": 0.1}, "slippage": NONE},
    {"fee": {"model": "per_contract", "maker": 0.1, "taker": -0.1}, "slippage": NONE},
    {"fee": {"model": "per_contract", "maker": -0.2, "taker": 0.1}, "slippage": NONE},
    {"fee": {"model": "per_contract", "maker": float("nan"), "taker": 0.1}, "slippage": NONE},
    {"fee": {"model": "per_contract", "maker": True, "taker": 0.1}, "slippage": NONE},
    {"fee": FEE, "slippage": {"model": "per_contract", "rate": -1.0}},
])
def test_a_bad_entry_is_refused(entry):
    with pytest.raises(CostConfigError):
        terms_for("X", entry)


def _config(profile):
    return {"datasets": {"d": {"symbol": "BTCUSDT"}}, "profiles": {"p": profile}}


def test_a_profile_cannot_state_a_rate_and_name_an_instrument():
    with pytest.raises(CostConfigError, match="one place"):
        apply_costs(_config({"dataset": "d", "costs": "BTCUSDT", "commission_bps": 1.0}))


def test_a_profile_cannot_be_priced_as_another_symbol():
    with pytest.raises(CostConfigError, match="priced as"):
        apply_costs(_config({"dataset": "d", "costs": "NQ"}))


def test_an_unknown_instrument_is_refused():
    with pytest.raises(CostConfigError, match="no instrument"):
        apply_costs(_config({"dataset": "d", "costs": "DOGEUSDT"}))


def test_a_profile_without_costs_is_untouched():
    config = {"profiles": {"p": {"commission_per_contract": 0.62}}}
    assert apply_costs(copy.deepcopy(config)) == config


def test_slippage_is_charged_on_takers_only():
    kw = dict(commission_per_contract=0.1, commission_bps=2.0, slippage_per_contract=0.25,
              slippage_bps=3.0)
    fee = 0.1 * 2 + 2e-4 * 100.0 * 2
    assert fill_commission(100.0, 2, **kw, liquidity="maker") == pytest.approx(fee)
    assert fill_commission(100.0, 2, **kw, liquidity="taker") == pytest.approx(
        fee + 0.25 * 2 + 3e-4 * 100.0 * 2)


def test_zero_slippage_is_the_engine_as_it_was():
    plain = _run()
    explicit = _run(slippage_per_contract=0.0, slippage_bps=0.0)
    assert explicit.blotter.digest() == plain.blotter.digest()
    assert "execution" not in explicit.record.as_dict()


def test_slippage_moves_cost_and_never_the_fill():
    plain, slipped = _run(), _run(slippage_per_contract=0.05, slippage_bps=1.0)
    assert slipped.fill_log_digest() == plain.fill_log_digest()
    assert slipped.blotter.realized_pnl == plain.blotter.realized_pnl
    extra = sum(0.05 * f.size + 1e-4 * f.price * f.size for f in slipped.fill_log
                if f.liquidity == "taker")
    assert extra > 0
    assert slipped.blotter.commission - plain.blotter.commission == pytest.approx(extra)
    assert slipped.reconciliation()["agree"]


def test_resting_limits_pay_no_slippage():
    plain, slipped = _run(RestingBids()), _run(RestingBids(), slippage_per_contract=1.0)
    assert plain.fill_log and all(f.liquidity == "maker" for f in plain.fill_log)
    assert slipped.blotter.commission == plain.blotter.commission


def test_the_overnight_close_pays_slippage():
    class Hold(RestingBids):
        def on_bars(self, ctx):
            if ctx.actionable and not ctx.position and not ctx.working_order_ids:
                ctx.buy(1, tif="GTC")

    engine = _run(Hold(), allow_overnight=False, slippage_per_contract=0.5)
    closes = [r for r in engine.blotter.rows if r.tag == "session-flat"]
    assert closes and all(r.commission == pytest.approx((0.1 + 0.5) * r.size) for r in closes)


def test_the_null_cannot_flip_slippage_into_a_gain():
    engine = _run(slippage_per_contract=0.05)
    calendar = engine.session_calendar()
    blotter = engine.reconstructed()
    plain = reconstruct(engine.fill_log, commission_per_contract=0.1)
    turns, base = rt.round_turns(blotter, calendar), rt.round_turns(plain, calendar)
    assert [t.gross for t in turns] == [t.gross for t in base]
    assert all(t.cost > b.cost for t, b in zip(turns, base))


def test_a_recorded_run_can_be_rebuilt():
    engine = _run(latency_ns=45_000_000_000, slippage_per_contract=0.05, slippage_bps=1.0)
    row = engine.record.as_dict()
    assert row_cost_terms(row)["slippage_bps"] == 1.0
    assert row_cost_terms({}) == {"maker_commission_per_contract": None,
                                  "maker_commission_bps": None,
                                  "slippage_per_contract": 0.0, "slippage_bps": 0.0}
    rebuilt = reconstruct(engine.fill_log, commission_per_contract=0.1, **row_cost_terms(row))
    assert rebuilt.digest() == engine.blotter.digest()


@pytest.mark.parametrize("kw", [{"slippage_bps": -1.0}, {"slippage_per_contract": float("nan")},
                                {"slippage_bps": True}])
def test_a_bad_slippage_is_refused(kw):
    with pytest.raises(ValueError, match="slippage"):
        Engine.for_test(_ds(), ReferenceStrategy(), split=Split.TRAIN, **kw)


def test_unsigned_slippage_is_not_applied(monkeypatch):
    import prereg

    monkeypatch.setattr(prereg, "in_force", lambda n, text=None: False)
    assert runner.execution_terms({"slippage_bps": 2.0}) == runner.DEFAULT_EXECUTION


def test_signed_slippage_is_applied(monkeypatch):
    import prereg

    monkeypatch.setattr(prereg, "in_force", lambda n, text=None: n == 19)
    assert runner.execution_terms({"slippage_bps": 2.0})["slippage_bps"] == 2.0



def test_maker_and_taker_fills_pay_their_own_rates():
    kw = dict(commission_per_contract=0.2, commission_bps=5.0,
              maker_commission_per_contract=0.05, maker_commission_bps=-1.0)
    assert fill_commission(100.0, 2, **kw, liquidity="taker") == pytest.approx(
        0.2 * 2 + 5e-4 * 200.0)
    assert fill_commission(100.0, 2, **kw, liquidity="maker") == pytest.approx(
        0.05 * 2 - 1e-4 * 200.0)


def test_a_rebate_is_allowed_up_to_the_taker_fee():
    terms = terms_for("X", {"fee": {"model": "bps_notional", "maker": -1.0, "taker": 4.0},
                            "slippage": {"model": "bps_notional", "rate": 0.0}})
    assert (terms["commission_bps"], terms["maker_commission_bps"]) == (4.0, -1.0)


def test_equal_maker_and_taker_rates_are_the_default_terms(monkeypatch):
    import prereg

    monkeypatch.setattr(prereg, "in_force", lambda n, text=None: False)
    profiles = runner.load_config()["profiles"]
    assert runner.execution_terms({**profiles["nq-train"]}) == runner.DEFAULT_EXECUTION


def test_explicit_equal_rates_are_the_engine_as_it_was():
    plain = _run(RestingBids())
    same = _run(RestingBids(), maker_commission_per_contract=None, maker_commission_bps=None)
    assert same.blotter.digest() == plain.blotter.digest()
    assert "execution" not in same.record.as_dict()


def test_a_maker_rate_changes_resting_fills_and_rebuilds_from_the_row():
    plain = _run(RestingBids())
    cheaper = _run(RestingBids(), maker_commission_per_contract=0.02)
    assert cheaper.fill_log_digest() == plain.fill_log_digest()
    assert cheaper.blotter.commission == pytest.approx(
        0.02 * sum(f.size for f in cheaper.fill_log))
    row = cheaper.record.as_dict()
    assert row["execution"]["maker_commission_per_contract"] == 0.02
    rebuilt = reconstruct(cheaper.fill_log, commission_per_contract=0.1, **row_cost_terms(row))
    assert rebuilt.digest() == cheaper.blotter.digest()
    assert cheaper.reconciliation()["agree"]


def test_a_taker_fill_ignores_the_maker_rate():
    plain = _run()
    maker_only = _run(maker_commission_per_contract=0.02)
    taker = [f for f in plain.fill_log if f.liquidity == "taker"]
    maker = [f for f in plain.fill_log if f.liquidity == "maker"]
    assert taker and maker
    assert maker_only.blotter.commission == pytest.approx(
        0.1 * sum(f.size for f in taker) + 0.02 * sum(f.size for f in maker))


@pytest.mark.parametrize("value", [float("nan"), True])
def test_a_bad_maker_rate_is_refused(value):
    with pytest.raises(ValueError, match="maker_commission_bps"):
        Engine.for_test(_ds(), ReferenceStrategy(), split=Split.TRAIN, maker_commission_bps=value)


def test_a_signed_profile_can_set_a_different_maker_rate(monkeypatch):
    import prereg

    monkeypatch.setattr(prereg, "in_force", lambda n, text=None: n == 19)
    terms = runner.execution_terms({"commission_bps": 5.0, "maker_commission_bps": 1.0})
    assert terms["maker_commission_bps"] == 1.0
