# Strategy Primitives & Signal Operator Library

`src/primitives` provides composable, point-in-time causal operators and an Abstract Syntax Tree (AST) evaluator for quantitative strategy signals.

When an agent proposes changes or adapts academic literature into trading hypotheses, using composable primitives avoids reinventing stateful buffers and eliminates subtle lookahead errors.

---

## 1. Core Guarantees

1. **Strict Point-in-Time Causality**  
   Every time-series operator runs strictly over trailing historical windows or positive lag periods ($t - k$). No operator ever reads future bars, forward-fills, or uses non-causal centered windows.
2. **Zero Execution Lookahead**  
   `compile_strategy_weights(ast_spec, data)` automatically applies a mandatory 1-bar execution shift (`.shift(1).fillna(0.0)`). A decision computed at bar $t$ executes at bar $t + 1$.
3. **Contract and Parameter Validation**  
   All window, period, span, and horizon parameters are validated at invocation time. Non-positive ($\le 0$) or non-integer values raise immediately with clear diagnostic errors.
4. **Bounded Complexity**  
   AST trees enforce a maximum depth (`max_depth = 6` by default). Runaway recursive structures fail validation before execution.
5. **Safe Arithmetic**  
   Operators handle zero division, zero variance, and non-positive logarithms by emitting `np.nan` rather than raising or leaking `inf` / `-inf`.

---

## 2. Available Primitives Catalog

### Time-Series Shifts and Differences
- `ts_delay(series, periods=1)`: Lag series by $k$ bars ($t - k$).
- `ts_delta(series, periods=1)`: Difference: $\text{series}_t - \text{series}_{t - k}$.
- `ts_returns(series, periods=1)`: Fractional return: $(\text{series}_t - \text{series}_{t-k}) / \text{series}_{t-k}$.

### Rolling Statistics and Moments
- `ts_mean(series, window)`: Trailing rolling mean.
- `ts_std(series, window)`: Trailing rolling sample standard deviation.
- `ts_var(series, window)`: Trailing rolling sample variance.
- `ts_zscore(series, window)`: Rolling z-score: $(\text{series} - \mu) / \sigma$. Safe against $\sigma = 0$.
- `ts_ema(series, span)`: Exponential moving average with given span.
- `ts_rsi(series, window=14)`: Wilder's Relative Strength Index (0 to 100).
- `ts_min(series, window)`: Trailing rolling minimum.
- `ts_max(series, window)`: Trailing rolling maximum.
- `ts_sum(series, window)`: Trailing rolling sum.
- `ts_median(series, window)`: Trailing rolling median.
- `ts_quantile(series, window, q=0.5)`: Trailing rolling quantile ($q \in [0, 1]$).
- `ts_rank(series, window)`: Trailing percentile rank of the current bar (0.0 to 1.0).

### Multivariate Operators
- `ts_corr(series1, series2, window)`: Trailing rolling Pearson correlation.
- `ts_cov(series1, series2, window)`: Trailing rolling covariance.

### Temporal Location
- `ts_argmax(series, window)`: Bars ago that maximum occurred in window ($0 = \text{current bar}$, $\text{window}-1 = \text{oldest bar}$).
- `ts_argmin(series, window)`: Bars ago that minimum occurred in window.

### Decays
- `ts_decay_linear(series, window)`: Linearly weighted moving average with normalized weights $[1, 2, \dots, \text{window}]$.
- `ts_decay_exp(series, span)`: Exponentially weighted moving average with span.

### Arithmetic & Comparison
- `add(a, b)`, `sub(a, b)`, `mul(a, b)`: Element-wise arithmetic.
- `div(a, b)`: Safe division; zero denominators emit `NaN`, not `inf`.
- `gt(a, b)`, `lt(a, b)`, `gte(a, b)`, `lte(a, b)`: Element-wise comparisons (returns 1.0 or 0.0).
- `eq(a, b)`, `neq(a, b)`: Element-wise equality checks.

### Logical & Non-Linear
- `and_op(a, b)`: Logical AND ($a > 0 \land b > 0$).
- `or_op(a, b)`: Logical OR ($a > 0 \lor b > 0$).
- `not_op(a)`: Logical NOT.
- `ts_where(condition, true_val, false_val)`: Multiplexer returning `true_val` when `condition > 0`, else `false_val`.
- `log(series)`: Natural logarithm (values $\le 0$ emit `NaN`).
- `abs(series)`: Absolute value.
- `sign(series)`: Sign function ($-1.0, 0.0, 1.0$).
- `signed_power(series, power=2.0)`: $\text{sign}(x) \cdot |x|^{\text{power}}$.
- `clip(series, lower=None, upper=None)`: Clamp series between bounds.

---

## 3. Abstract Syntax Tree (AST) Format

An expression is represented as a nested Python dictionary:

### Field Node
Reads a named column from the market data DataFrame (case-insensitive):
```json
{
  "type": "field",
  "name": "close"
}
```

### Constant Node
Injects a fixed numeric scalar:
```json
{
  "type": "constant",
  "value": 20.0
}
```

### Operator Node
Applies a registered primitive operator:
```json
{
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
```

---

## 4. Compiling and Executing Strategy Weights

```python
from src.primitives import evaluate_node, compile_strategy_weights, validate_ast

ast_spec = {
    "type": "operator",
    "name": "ts_zscore",
    "args": [{"type": "field", "name": "close"}],
    "kwargs": {"window": 20}
}

# 1. Statically validate before running
is_valid, error = validate_ast(ast_spec)
assert is_valid, error

# 2. Compile causal strategy weights over a bar DataFrame
# Target weights are normalized via tanh to [-1.0, 1.0] and lagged by 1 bar
weights = compile_strategy_weights(ast_spec, bars_df)
```

---

## 5. Introspection

Agents can dynamically inspect available primitives and signatures:

```python
from src.primitives import list_primitives, describe_primitives

all_ops = list_primitives()
# ['abs', 'add', 'and_op', 'clip', 'div', 'eq', 'gt', 'gte', 'log', ...]

details = describe_primitives()
print(details["ts_rsi"]["signature"])
# (series: pandas.core.series.Series, window: int = 14) -> pandas.core.series.Series
```
