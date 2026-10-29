"""Dynamic strategy module loading by path (Task 1.4)."""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path
import sys

from src.strategy.protocol import Strategy


class NoStrategyFoundError(Exception):
    """Raised when a module contains zero or more than one conforming class."""


def load_strategy(module_path: str) -> Strategy:
    """Import the module at ``module_path`` and return its ``Strategy`` instance.

    ``module_path`` is either a dotted module path (e.g. ``pkg.mod``) or a
    filesystem path to a ``.py`` file. The module must contain exactly one class
    structurally conforming to :class:`Strategy`.
    """
    module = _import_module(module_path)

    conforming: list[type] = []
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if obj is Strategy:
            continue
        try:
            if issubclass(obj, Strategy) and obj.__module__ == module.__name__:
                conforming.append(obj)
        except TypeError:
            continue

    if len(conforming) != 1:
        n = len(conforming)
        raise NoStrategyFoundError(
            f"expected exactly one Strategy in {module_path}, found {n}"
        )

    return conforming[0]()


def _import_module(module_path: str):
    path = Path(module_path)
    if path.exists() and path.suffix == ".py":
        spec_path = str(path.resolve())
        name = f"_strategy_{path.stem}_{abs(hash(spec_path))}"
        spec = importlib.util.spec_from_file_location(name, spec_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    return importlib.import_module(module_path)
