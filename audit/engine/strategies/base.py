"""Strategy interface: signal in [-1, 1] from a point-in-time close series."""

from __future__ import annotations

from typing import Protocol


class Strategy(Protocol):
    name: str

    def signal(self, closes: list[float]) -> list[float]:
        """Return a position target per bar.

        Implementations must use only closes[0:t+1] when filling index t.
        Using closes[t+1] is look-ahead and will fail leak audit.
        """
