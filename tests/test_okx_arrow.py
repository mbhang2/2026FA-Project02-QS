"""The Arrow trade store is exact, read-only and read without copying."""

from __future__ import annotations

import numpy as np
import pytest

from src.core.data.okx_book import read_trade_arrow, write_trade_arrow


def arrays():
    return (np.array([1, 2, 2**62], dtype=np.int64), np.array([100.1, 100.2, 111987.5]),
            np.array([1, 5, 5_955_200], dtype=np.int64), np.array([1, -1, 1], dtype=np.int8))


def test_a_round_trip_is_exact(tmp_path):
    path = tmp_path / "day.arrow"
    write_trade_arrow(path, arrays())
    for want, got in zip(arrays(), read_trade_arrow(path)):
        assert got.dtype == want.dtype and np.array_equal(got, want)


def test_the_store_is_read_only(tmp_path):
    path = tmp_path / "day.arrow"
    write_trade_arrow(path, arrays())
    for column in read_trade_arrow(path):
        assert not column.flags.writeable
        with pytest.raises(ValueError):
            column[0] = 0


def test_columns_are_views_of_the_mapped_file(tmp_path):
    path = tmp_path / "day.arrow"
    write_trade_arrow(path, arrays())
    for column in read_trade_arrow(path):
        assert not column.flags.owndata


def test_a_failed_write_leaves_no_store(tmp_path):
    path = tmp_path / "day.arrow"
    with pytest.raises(Exception):
        write_trade_arrow(path, (np.array(["bad"]), np.zeros(1), np.zeros(1), np.zeros(1)))
    assert not path.exists()
    assert not list(tmp_path.glob("*.tmp"))
