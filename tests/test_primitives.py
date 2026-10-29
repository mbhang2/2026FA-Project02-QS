import pytest
import pandas as pd
import numpy as np
from src.primitives import (
    PRIMITIVE_REGISTRY,
    register_primitive,
    list_primitives,
    describe_primitives,
    evaluate_node,
    compile_strategy_weights,
    validate_ast,
)


@pytest.fixture
def sample_data():
    np.random.seed(42)
    dates = pd.date_range("2020-01-01", periods=100)
    df = pd.DataFrame({
        "open": np.random.randn(100).cumsum() + 100,
        "high": np.random.randn(100).cumsum() + 105,
        "low": np.random.randn(100).cumsum() + 95,
        "close": np.random.randn(100).cumsum() + 100,
        "volume": np.random.randint(1000, 10000, 100)
    }, index=dates)
    return df


def test_registry_contract_validation():
    op_func = PRIMITIVE_REGISTRY["ts_mean"]
    s = pd.Series([1, 2, 3, 4, 5])

    # Valid call
    res = op_func(s, window=2)
    assert len(res) == 5

    # Non-positive parameters
    with pytest.raises(ValueError, match="strictly positive"):
        op_func(s, window=0)

    with pytest.raises(ValueError, match="strictly positive"):
        op_func(s, window=-1)

    # Non-integer parameters
    with pytest.raises(TypeError, match="must be an integer"):
        op_func(s, window=2.5)

    with pytest.raises(TypeError, match="must be an integer"):
        op_func(s, window=True)

    delay_func = PRIMITIVE_REGISTRY["ts_delay"]
    with pytest.raises(ValueError, match="strictly positive"):
        delay_func(s, periods=0)


def test_registry_introspection():
    prims = list_primitives()
    assert isinstance(prims, list)
    assert "ts_mean" in prims
    assert "ts_rsi" in prims
    assert "ts_zscore" in prims

    descriptions = describe_primitives()
    assert "ts_mean" in descriptions
    assert "signature" in descriptions["ts_mean"]
    assert "doc" in descriptions["ts_mean"]


def test_evaluator_correctness(sample_data):
    # AST: zscore of 20-bar SMA with window 10
    ast = {
        "type": "operator",
        "name": "ts_zscore",
        "args": [
            {
                "type": "operator",
                "name": "ts_mean",
                "args": [{"type": "field", "name": "close"}],
                "kwargs": {"window": 20}
            }
        ],
        "kwargs": {"window": 10}
    }

    result = evaluate_node(ast, sample_data)

    # Manual calculation
    manual_mean = sample_data["close"].rolling(20).mean()
    manual_std = manual_mean.rolling(10).std().replace(0.0, np.nan)
    manual_zscore = (manual_mean - manual_mean.rolling(10).mean()) / manual_std

    pd.testing.assert_series_equal(result, manual_zscore)


def test_point_in_time_causality(sample_data):
    # Test causality across multiple operators
    operators_to_test = [
        {"type": "operator", "name": "ts_rsi", "args": [{"type": "field", "name": "close"}], "kwargs": {"window": 14}},
        {"type": "operator", "name": "ts_mean", "args": [{"type": "field", "name": "close"}], "kwargs": {"window": 10}},
        {"type": "operator", "name": "ts_zscore", "args": [{"type": "field", "name": "close"}], "kwargs": {"window": 10}},
        {"type": "operator", "name": "ts_ema", "args": [{"type": "field", "name": "close"}], "kwargs": {"span": 12}},
        {"type": "operator", "name": "ts_decay_linear", "args": [{"type": "field", "name": "close"}], "kwargs": {"window": 5}},
        {"type": "operator", "name": "ts_returns", "args": [{"type": "field", "name": "close"}], "kwargs": {"periods": 2}},
        {"type": "operator", "name": "ts_argmax", "args": [{"type": "field", "name": "close"}], "kwargs": {"window": 5}},
    ]

    t = 50
    truncated_data = sample_data.iloc[:t + 1]

    for ast in operators_to_test:
        full_result = evaluate_node(ast, sample_data)
        truncated_result = evaluate_node(ast, truncated_data)

        # The value at t should be bit-identical between full and truncated
        val_full = full_result.iloc[t]
        val_trunc = truncated_result.iloc[t]
        assert np.isclose(val_full, val_trunc, equal_nan=True), f"Failed causality for {ast['name']}"


def test_execution_shift_and_leakage(sample_data):
    ast = {
        "type": "operator",
        "name": "ts_mean",
        "args": [{"type": "field", "name": "close"}],
        "kwargs": {"window": 5}
    }

    raw_alpha = evaluate_node(ast, sample_data)
    weights = compile_strategy_weights(ast, sample_data)

    target_weights = np.tanh(raw_alpha).clip(-1.0, 1.0)
    expected_weights = target_weights.shift(1).fillna(0.0)

    pd.testing.assert_series_equal(weights, expected_weights)

    # First bar must be zero because of shift(1)
    assert weights.iloc[0] == 0.0
    assert weights.iloc[5] == expected_weights.iloc[5]


def test_max_depth(sample_data):
    ast = {
        "type": "operator",
        "name": "ts_delay",
        "args": [
            {
                "type": "operator",
                "name": "ts_delay",
                "args": [{"type": "field", "name": "close"}],
                "kwargs": {"periods": 1}
            }
        ],
        "kwargs": {"periods": 1}
    }

    # Should work with max_depth=3 (depth 1=outer, 2=inner, 3=field)
    evaluate_node(ast, sample_data, max_depth=3)

    # Should fail with max_depth=2
    with pytest.raises(ValueError, match="Tree depth exceeds"):
        evaluate_node(ast, sample_data, max_depth=2)


def test_invalid_field(sample_data):
    ast = {"type": "field", "name": "invalid_column"}
    with pytest.raises(KeyError):
        evaluate_node(ast, sample_data)


def test_arithmetic_operators(sample_data):
    ast = {
        "type": "operator",
        "name": "div",
        "args": [
            {
                "type": "operator",
                "name": "add",
                "args": [{"type": "field", "name": "high"}, {"type": "field", "name": "low"}]
            },
            {"type": "constant", "value": 2.0}
        ]
    }

    result = evaluate_node(ast, sample_data)
    expected = (sample_data["high"] + sample_data["low"]) / 2.0
    pd.testing.assert_series_equal(result, expected)


def test_safe_division(sample_data):
    ast = {
        "type": "operator",
        "name": "div",
        "args": [
            {"type": "field", "name": "close"},
            {"type": "constant", "value": 0.0}
        ]
    }
    result = evaluate_node(ast, sample_data)
    assert result.isna().all()


def test_multivariate_operators(sample_data):
    ast = {
        "type": "operator",
        "name": "ts_corr",
        "args": [
            {"type": "field", "name": "high"},
            {"type": "field", "name": "low"}
        ],
        "kwargs": {"window": 10}
    }

    result = evaluate_node(ast, sample_data)
    expected = sample_data["high"].rolling(window=10).corr(sample_data["low"])
    pd.testing.assert_series_equal(result, expected)


def test_ts_rank(sample_data):
    ast = {
        "type": "operator",
        "name": "ts_rank",
        "args": [{"type": "field", "name": "close"}],
        "kwargs": {"window": 5}
    }

    result = evaluate_node(ast, sample_data)
    assert not result.isna().all()
    valid_ranks = result.dropna()
    assert (valid_ranks >= 0.0).all() and (valid_ranks <= 1.0).all()


def test_logical_operators(sample_data):
    ast_gt = {
        "type": "operator",
        "name": "gt",
        "args": [{"type": "field", "name": "high"}, {"type": "field", "name": "low"}]
    }
    result_gt = evaluate_node(ast_gt, sample_data)
    expected_gt = (sample_data["high"] > sample_data["low"]).astype(float)
    pd.testing.assert_series_equal(result_gt, expected_gt)

    ast_where = {
        "type": "operator",
        "name": "ts_where",
        "args": [
            ast_gt,
            {"type": "field", "name": "high"},
            {"type": "field", "name": "low"}
        ]
    }
    result_where = evaluate_node(ast_where, sample_data)
    expected_where = pd.Series(
        np.where(expected_gt > 0, sample_data["high"], sample_data["low"]),
        index=sample_data.index
    )
    pd.testing.assert_series_equal(result_where, expected_where)


def test_comparisons_and_boolean_logic(sample_data):
    ast_gte = {
        "type": "operator", "name": "gte",
        "args": [{"type": "field", "name": "close"}, {"type": "field", "name": "open"}]
    }
    res_gte = evaluate_node(ast_gte, sample_data)
    expected_gte = (sample_data["close"] >= sample_data["open"]).astype(float)
    pd.testing.assert_series_equal(res_gte, expected_gte)

    ast_and = {
        "type": "operator", "name": "and_op",
        "args": [ast_gte, {"type": "constant", "value": 1.0}]
    }
    res_and = evaluate_node(ast_and, sample_data)
    pd.testing.assert_series_equal(res_and, expected_gte)

    ast_not = {
        "type": "operator", "name": "not_op",
        "args": [ast_gte]
    }
    res_not = evaluate_node(ast_not, sample_data)
    pd.testing.assert_series_equal(res_not, (1.0 - expected_gte))


def test_temporal_operators(sample_data):
    ast_argmax = {
        "type": "operator",
        "name": "ts_argmax",
        "args": [{"type": "field", "name": "close"}],
        "kwargs": {"window": 5}
    }
    result = evaluate_node(ast_argmax, sample_data)
    assert result.max() <= 4  # max index for window 5 is 4 bars ago
    assert result.min() >= 0

    ast_argmin = {
        "type": "operator",
        "name": "ts_argmin",
        "args": [{"type": "field", "name": "close"}],
        "kwargs": {"window": 5}
    }
    res_argmin = evaluate_node(ast_argmin, sample_data)
    assert res_argmin.max() <= 4
    assert res_argmin.min() >= 0


def test_nonlinear_operators(sample_data):
    ast_abs = {
        "type": "operator",
        "name": "abs",
        "args": [
            {
                "type": "operator",
                "name": "ts_delta",
                "args": [{"type": "field", "name": "close"}],
                "kwargs": {"periods": 1}
            }
        ]
    }
    result = evaluate_node(ast_abs, sample_data)
    expected = (sample_data["close"] - sample_data["close"].shift(1)).abs()
    pd.testing.assert_series_equal(result, expected)

    ast_sign = {"type": "operator", "name": "sign", "args": [{"type": "field", "name": "close"}]}
    res_sign = evaluate_node(ast_sign, sample_data)
    pd.testing.assert_series_equal(res_sign, np.sign(sample_data["close"]))

    ast_power = {
        "type": "operator", "name": "signed_power",
        "args": [{"type": "field", "name": "close"}],
        "kwargs": {"power": 2.0}
    }
    res_power = evaluate_node(ast_power, sample_data)
    expected_power = np.sign(sample_data["close"]) * (sample_data["close"].abs() ** 2.0)
    pd.testing.assert_series_equal(res_power, expected_power)


def test_linear_decay(sample_data):
    ast = {
        "type": "operator",
        "name": "ts_decay_linear",
        "args": [{"type": "field", "name": "close"}],
        "kwargs": {"window": 3}
    }
    result = evaluate_node(ast, sample_data)

    w = np.array([1, 2, 3]) / 6.0
    expected_val = np.dot(sample_data["close"].iloc[0:3].values, w)
    assert np.isclose(result.iloc[2], expected_val)


def test_validate_ast():
    # Valid AST
    valid = {
        "type": "operator",
        "name": "ts_mean",
        "args": [{"type": "field", "name": "close"}],
        "kwargs": {"window": 10}
    }
    ok, err = validate_ast(valid)
    assert ok and err == ""

    # Invalid node type
    invalid_type = {"type": "unknown"}
    ok, err = validate_ast(invalid_type)
    assert not ok and "Unknown node type" in err

    # Missing field name
    bad_field = {"type": "field"}
    ok, err = validate_ast(bad_field)
    assert not ok and "name" in err

    # Unknown operator
    bad_op = {"type": "operator", "name": "nonexistent_op", "args": []}
    ok, err = validate_ast(bad_op)
    assert not ok and "Unknown operator" in err

    # Non-numeric constant
    bad_const = {"type": "constant", "value": "string"}
    ok, err = validate_ast(bad_const)
    assert not ok and "numeric" in err

    # Exceeded depth
    deep = {"type": "operator", "name": "abs", "args": [
        {"type": "operator", "name": "abs", "args": [
            {"type": "field", "name": "close"}
        ]}
    ]}
    ok, err = validate_ast(deep, max_depth=2)
    assert not ok and "exceeds allowed limit" in err


def test_constant_unwrapping_in_kwargs(sample_data):
    # kwargs with constant node unwrapped
    ast = {
        "type": "operator",
        "name": "ts_mean",
        "args": [{"type": "field", "name": "close"}],
        "kwargs": {"window": {"type": "constant", "value": 10}}
    }
    result = evaluate_node(ast, sample_data)
    expected = sample_data["close"].rolling(10).mean()
    pd.testing.assert_series_equal(result, expected)
