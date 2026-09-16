"""Tests for the no-lookahead leakage guard (Task 2.1)."""

from __future__ import annotations

import pandas as pd
import pytest

from nalin_l1.execution.leakage_guard import LookaheadViolation, assert_no_lookahead


def test_no_lookahead_passes_when_clean():
    idx = pd.date_range("2024-01-01", periods=3, tz="UTC")
    df = pd.DataFrame({"close": [1, 2, 3]}, index=idx)
    assert_no_lookahead(df, pd.Timestamp("2024-01-03", tz="UTC"))


def test_no_lookahead_raises_on_future_row():
    idx = pd.date_range("2024-01-01", periods=3, tz="UTC")
    df = pd.DataFrame({"close": [1, 2, 3]}, index=idx)
    with pytest.raises(LookaheadViolation):
        assert_no_lookahead(df, pd.Timestamp("2024-01-02", tz="UTC"))


def test_no_lookahead_reports_offending_timestamp():
    idx = pd.date_range("2024-01-01", periods=3, tz="UTC")
    df = pd.DataFrame({"close": [1, 2, 3]}, index=idx)
    with pytest.raises(LookaheadViolation) as exc:
        assert_no_lookahead(df, pd.Timestamp("2024-01-01", tz="UTC"))
    assert "2024-01-02" in str(exc.value)
