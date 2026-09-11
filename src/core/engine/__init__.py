"""The simulator: event loop, order lifecycle, fill models, bars, context.

The immutable side of the split. A strategy calls into `context`; everything else
here is machinery it cannot reach.
"""

from .bars import BarBuilder
from .context import (
    BarContext,
    ContextError,
    ContextExpired,
    EnginePort,
    FillContext,
    IntentQueue,
    LifecycleContext,
    SetupContext,
    TickContext,
    canonical_sub_id,
    parse_interval,
)
from .engine import (
    BudgetExceeded,
    Engine,
    RunRecord,
    aggregate,
    git_sha,
    validate_strategy,
)
from .fills import FavorableFill, FillModel, UnfavorableFill, get_fill_model
from .view import BarHistory, HistoryView

__all__ = [
    "BarBuilder",
    "BarContext",
    "BarHistory",
    "BudgetExceeded",
    "ContextError",
    "ContextExpired",
    "EnginePort",
    "Engine",
    "FavorableFill",
    "FillContext",
    "FillModel",
    "HistoryView",
    "IntentQueue",
    "LifecycleContext",
    "RunRecord",
    "SetupContext",
    "TickContext",
    "UnfavorableFill",
    "aggregate",
    "canonical_sub_id",
    "get_fill_model",
    "git_sha",
    "parse_interval",
    "validate_strategy",
]
