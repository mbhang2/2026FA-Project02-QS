"""Static analysis of the agent's file. Nothing here executes strategy code.

Re-exported so the entry point and the tests import one name:
`from src.core.checks import static_check, type_check`.
"""

from .static import Finding, StaticReport, static_check
from .typecheck import TypeResult, type_check

__all__ = ["Finding", "StaticReport", "static_check", "TypeResult", "type_check"]
