"""Registry and contract enforcement for quantitative strategy primitives."""

from typing import Callable, Any
import functools
import inspect

PRIMITIVE_REGISTRY: dict[str, Callable] = {}


def register_primitive(name: str):
    """Decorator to register a strategy primitive function with input contract validation."""
    def decorator(func: Callable):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Inspect signature to find parameters related to windows, periods, horizons
            sig = inspect.signature(func)
            bound_args = sig.bind(*args, **kwargs)
            bound_args.apply_defaults()

            for param_name, param_value in bound_args.arguments.items():
                if param_name in ("window", "periods", "span", "horizon", "period"):
                    if isinstance(param_value, bool) or not isinstance(param_value, int):
                        raise TypeError(
                            f"Parameter '{param_name}' must be an integer. Got: {type(param_value).__name__}"
                        )
                    if param_value < 1:
                        raise ValueError(
                            f"Parameter '{param_name}' must be strictly positive (>= 1). Got: {param_value}"
                        )

            return func(*args, **kwargs)

        PRIMITIVE_REGISTRY[name.lower()] = wrapper
        return wrapper
    return decorator


def list_primitives() -> list[str]:
    """Return a sorted list of registered primitive names."""
    return sorted(PRIMITIVE_REGISTRY.keys())


def describe_primitives() -> dict[str, dict[str, Any]]:
    """Return introspection details (signature and docstring) for all registered primitives."""
    result = {}
    for name, func in sorted(PRIMITIVE_REGISTRY.items()):
        sig = str(inspect.signature(func))
        doc = inspect.getdoc(func) or ""
        result[name] = {
            "signature": sig,
            "doc": doc,
        }
    return result
