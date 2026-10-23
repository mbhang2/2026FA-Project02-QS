"""Abstract Syntax Tree (AST) evaluator and strategy compiler for signal primitives.

Evaluates structured expression trees over market data frames and compiles
causal portfolio / target weights with guaranteed 1-bar execution lag.
"""

from typing import Any
import pandas as pd
import numpy as np
from src.primitives.registry import PRIMITIVE_REGISTRY


def validate_ast(node: dict[str, Any], current_depth: int = 1, max_depth: int = 6) -> tuple[bool, str]:
    """Statically validate the structure of an AST expression tree without requiring data."""
    if not isinstance(node, dict):
        return False, f"Node must be a dictionary, got {type(node).__name__}"

    if current_depth > max_depth:
        return False, f"Tree depth {current_depth} exceeds allowed limit of {max_depth}"

    node_type = node.get("type")
    if not node_type:
        return False, "Node missing required 'type' field"

    if node_type == "field":
        name = node.get("name")
        if not name or not isinstance(name, str):
            return False, "Field node must have a non-empty string 'name'"
        return True, ""

    elif node_type == "constant":
        if "value" not in node:
            return False, "Constant node missing 'value' field"
        val = node.get("value")
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            return False, f"Constant value must be numeric, got {type(val).__name__}"
        return True, ""

    elif node_type == "operator":
        op_name = node.get("name")
        if not op_name or not isinstance(op_name, str):
            return False, "Operator node must have a non-empty string 'name'"

        if op_name.lower() not in PRIMITIVE_REGISTRY:
            return False, f"Unknown operator '{op_name}'. Available: {sorted(PRIMITIVE_REGISTRY.keys())}"

        args = node.get("args", [])
        if not isinstance(args, (list, tuple)):
            return False, "Operator 'args' must be a list or tuple"

        for arg in args:
            ok, err = validate_ast(arg, current_depth=current_depth + 1, max_depth=max_depth)
            if not ok:
                return False, err

        kwargs = node.get("kwargs", {})
        if not isinstance(kwargs, dict):
            return False, "Operator 'kwargs' must be a dictionary"

        for k, v in kwargs.items():
            if isinstance(v, dict) and "type" in v:
                ok, err = validate_ast(v, current_depth=current_depth + 1, max_depth=max_depth)
                if not ok:
                    return False, err

        return True, ""

    else:
        return False, f"Unknown node type '{node_type}'"


def evaluate_node(
    node: dict[str, Any],
    data: pd.DataFrame,
    current_depth: int = 1,
    max_depth: int = 6
) -> pd.Series:
    """Evaluate an AST node against historical DataFrame columns in a point-in-time causal manner."""
    if current_depth > max_depth:
        raise ValueError("Tree depth exceeds allowed complexity limit")

    node_type = node.get("type")

    if node_type == "field":
        field_name = node.get("name")
        if not field_name:
            raise ValueError("Field node must have 'name'")

        # Case-insensitive column matching
        col_match = None
        for col in data.columns:
            if str(col).lower() == str(field_name).lower():
                col_match = col
                break

        if col_match is None:
            raise KeyError(f"Column '{field_name}' not found in dataframe")

        return data[col_match].astype(float)

    elif node_type == "constant":
        val = node.get("value")
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise ValueError(f"Constant node must have numeric 'value'. Got: {type(val)}")
        return pd.Series(float(val), index=data.index)

    elif node_type == "operator":
        op_name = node.get("name")
        if not op_name:
            raise ValueError("Operator node must have 'name'")

        op_name_lower = op_name.lower()
        if op_name_lower not in PRIMITIVE_REGISTRY:
            raise ValueError(f"Unknown operator: {op_name}")

        op_func = PRIMITIVE_REGISTRY[op_name_lower]

        args = [
            evaluate_node(arg, data, current_depth=current_depth + 1, max_depth=max_depth)
            for arg in node.get("args", [])
        ]

        kwargs = {}
        for k, v in node.get("kwargs", {}).items():
            if isinstance(v, dict) and v.get("type") == "constant":
                # Unwrap numeric constants directly into primitive kwargs
                kwargs[k] = v.get("value")
            elif isinstance(v, dict) and "type" in v:
                kwargs[k] = evaluate_node(v, data, current_depth=current_depth + 1, max_depth=max_depth)
            else:
                kwargs[k] = v

        return op_func(*args, **kwargs)

    else:
        raise ValueError(f"Unknown node type: {node_type}")


def compile_strategy_weights(ast_spec: dict[str, Any], data: pd.DataFrame) -> pd.Series:
    """Evaluate AST expression into normalized target weights with 1-bar execution shift.

    1. Evaluates raw alpha series from AST node over bar data.
    2. Applies hyperbolic tangent bounding: tanh(raw_alpha) clipped to [-1.0, 1.0].
    3. Shifts resulting position target by 1 bar (.shift(1).fillna(0.0)).

    This guarantees that the executed weight at bar t depends strictly on data
    observed up to bar t - 1, completely eliminating execution-time lookahead leakage.
    """
    raw_alpha = evaluate_node(ast_spec, data)

    # Normalize via tanh and clip to [-1.0, 1.0]
    target_weights = np.tanh(raw_alpha).clip(-1.0, 1.0)

    # Execution shift by 1 bar to prevent leakage and guarantee strict causality
    executed_weights = target_weights.shift(1).fillna(0.0)

    return executed_weights
