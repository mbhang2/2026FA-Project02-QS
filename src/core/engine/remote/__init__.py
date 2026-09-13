"""Run the strategy in its own process. See `docs/design/architecture.md` §5c."""

from .adapter import (
    HarnessFailed, Isolation, RemoteFailure, RemoteStrategy, StrategyExceededResources,
    StrategyRaised, StrategyTimedOut, StrategyViolatedProtocol, isolation_for,
)
from .protocol import ProtocolError, SetupPayloadTooLarge

__all__ = [
    "RemoteStrategy", "Isolation", "isolation_for",
    "RemoteFailure", "StrategyRaised", "StrategyTimedOut",
    "StrategyExceededResources", "StrategyViolatedProtocol", "HarnessFailed",
    "ProtocolError", "SetupPayloadTooLarge",
]
