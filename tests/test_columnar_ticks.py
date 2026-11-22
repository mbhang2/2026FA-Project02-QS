"""Columnar ticks are an optimisation: every run must match the object path exactly."""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import numpy as np
import pytest

from src.core.data.columns import SessionColumns
from src.core.data.dataset import HoldoutLock, HoldoutLocked, sessions_for
from src.core.data.nq import NQDataset
from src.core.data.synthetic import SyntheticDataset, SyntheticSpec
from src.core.engine import Engine
from src.core.engine.engine import _SealedDataset
from src.core.scoring.calibration.reference import ReferenceStrategy
from src.core.scoring.randomization.rotation import RotatedDataset
from src.core.types import Side, Split, Tick


class ColumnarDataset(SyntheticDataset):
    """The same synthetic trades, also served as columns."""

    served = 0

    def session_columns(self, split, *, warmup: int = 0):
        self._lock.check(split)
        warm, live = sessions_for(self._calendar, split, warmup)
        for s in list(warm) + list(live):
            ticks = self._session_ticks(s)
            if not ticks:
                continue
            type(self).served += 1
            yield SessionColumns(
                s, np.array([t.ts for t in ticks], dtype=np.int64),
                np.array([t.price for t in ticks], dtype=float),
                np.array([t.size for t in ticks], dtype=np.int64),
                np.array([1 if t.side is Side.BUY else -1 if t.side is Side.SELL else 0
                          for t in ticks], dtype=np.int8))


class TickFollower:
    """Subscribes to every tick, so the walk steps every column."""

    def setup(self, ctx):
        ctx.subscribe("tick")
        ctx.subscribe("5m")

    def on_tick(self, ctx):
        if not ctx.position and not ctx.working_order_ids and ctx.ts % 7 == 0:
            ctx.buy(size=1, stop_offset=0.5, target_offset=0.5)

    def on_bars(self, ctx):
        pass

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass


def _spec(**kw) -> SyntheticSpec:
    return SyntheticSpec(**{"n_sessions": 40, "ticks_per_session": 240, "vol_per_tick": 0.15,
                            "seed": 3, **kw})


def _run(dataset, strategy, **kw):
    engine = Engine.for_test(dataset, strategy, split=Split.TRAIN, warmup_sessions=1, seed=1, **kw)
    engine.run()
    return engine


@pytest.mark.parametrize("factory", [ReferenceStrategy, TickFollower])
@pytest.mark.parametrize("event_walk", [True, False])
def test_columns_and_objects_produce_the_same_run(factory, event_walk):
    ColumnarDataset.served = 0
    objects = _run(SyntheticDataset(_spec()), factory(), event_walk=event_walk)
    columns = _run(ColumnarDataset(_spec()), factory(), event_walk=event_walk)
    assert ColumnarDataset.served > 0                 # the fast path really ran
    assert objects.blotter.rows, "the scenario has to trade to prove anything"
    assert columns.blotter.digest() == objects.blotter.digest()
    assert (columns.n_ticks, columns.n_stepped) == (objects.n_ticks, objects.n_stepped)
    assert columns.blotter.final_position == objects.blotter.final_position
    assert columns.blotter.final_price == objects.blotter.final_price


def test_indexing_gives_the_tick_the_dataset_would_serve():
    ds = ColumnarDataset(_spec())
    batch = next(ds.session_columns(Split.TRAIN))
    want = ds._session_ticks(batch.session)
    assert len(batch) == len(want)
    assert all(batch[i] == Tick(t.ts, t.price, t.size, t.session, side=t.side)
               for i, t in enumerate(want))


def test_an_unknown_aggressor_stays_unknown():
    c = SessionColumns(5, np.array([1, 2, 3]), np.array([1.0, 2.0, 3.0]), np.array([1, 1, 1]),
                       np.array([1, -1, 0], dtype=np.int8))
    assert [c[i].side for i in range(3)] == [Side.BUY, Side.SELL, None]


@pytest.mark.parametrize("arrays", [
    ([], [], [], []),
    ([1, 2], [1.0], [1, 1], [1, 1]),
])
def test_malformed_columns_are_refused(arrays):
    with pytest.raises(ValueError):
        SessionColumns(1, *(np.array(a) for a in arrays))


def test_a_wrapper_that_changes_ticks_cannot_forward_columns():
    inner = ColumnarDataset(_spec())
    rotated = RotatedDataset(inner=inner, seed=1)
    assert rotated.session_columns.__self__ is inner   # it really does forward them
    assert Engine._columns_source(rotated) is None
    assert Engine._columns_source(_SealedDataset(rotated)) is None
    assert Engine._columns_source(_SealedDataset(inner)) is not None


def test_a_rotated_run_is_the_rotated_data_not_the_original():
    plain = RotatedDataset(inner=SyntheticDataset(_spec()), seed=1)
    columnar = RotatedDataset(inner=ColumnarDataset(_spec()), seed=1)
    assert _run(columnar, ReferenceStrategy()).blotter.digest() \
        == _run(plain, ReferenceStrategy()).blotter.digest()


class _CachedNQ(NQDataset):
    """One cached NQ session, with no bucket behind it."""

    def __init__(self, path, day):
        self.spec = SimpleNamespace(cache=True, smoke_sessions=None, symbol="NQ")
        self.path, self._sessions, self._files = path, [day], {day: "key"}
        self._contracts, self._lock = {}, HoldoutLock()

    def _cache_path(self, session):
        return self.path

    def _split_positions(self, split, warmup):
        return [], [0]


def test_nq_columns_are_the_ticks_the_cache_serves(tmp_path):
    day = dt.date(2025, 3, 4)
    path = tmp_path / "session.npz"
    meta = {"session": day.isoformat(), "instrument_id": 7, "raw_symbol": "NQH5", "volume": 9,
            "trades": 4, "share_of_session_volume": 1.0}
    np.savez_compressed(path, ts=np.array([10, 11, 12, 13], dtype=np.int64),
                        price=np.array([20_000.25, 20_000.5, 20_000.0, 20_000.75]),
                        size=np.array([1, 2, 3, 3], dtype=np.int32),
                        side=np.array([1, 2, 0, 3], dtype=np.int8), meta=json.dumps(meta))
    ds = _CachedNQ(path, day)
    want = ds.load(day)
    batch = next(ds.session_columns(Split.TRAIN))
    assert [batch[i] for i in range(len(batch))] == want
    assert [t.side for t in want] == [Side.BUY, Side.SELL, None, None]
    assert batch.size.dtype == np.int64
    assert ds.contract_for(day).raw_symbol == "NQH5"
    with pytest.raises(HoldoutLocked):
        next(ds.session_columns(Split.HOLDOUT))


def test_the_holdout_stays_locked():
    ds = ColumnarDataset(_spec(n_sessions=60))
    with pytest.raises(Exception, match="(?i)holdout"):
        next(ds.session_columns(Split.HOLDOUT))
