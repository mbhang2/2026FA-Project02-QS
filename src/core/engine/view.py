"""The read surface a strategy gets, and the reason it is not a plain list.

Make the bug unrepresentable rather than documented: a prompt telling an agent to
avoid lookahead does not prevent lookahead, an API that cannot express it does.
`HistoryView` holds only what has already closed, and reaching at or beyond the
cursor raises `LookaheadError` rather than returning something plausible.
"""

from __future__ import annotations

from typing import Generic, Iterator, Sequence, TypeVar

from ..types import Bar, LookaheadError

T = TypeVar("T")


class HistoryView(Generic[T]):
    """A window over closed observations, bounded at the current instant.

    Positive indices count forward from the start, negative from the most recent
    closed element, so `view[-1]` is the bar that just closed. `view[len(view)]`
    -- the element that does not exist yet -- raises. So does any slice whose
    stop reaches past the cursor.
    """

    __slots__ = ("_items",)

    def __init__(self, items: Sequence[T] | None = None):
        self._items: list[T] = list(items or ())

    # the engine appends; strategies never do
    def _append(self, item: T) -> None:
        self._items.append(item)

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[T]:
        return iter(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)

    def __getitem__(self, key):
        n = len(self._items)
        if isinstance(key, slice):
            stop = key.stop
            if stop is not None and stop >= 0 and stop > n:
                raise LookaheadError(
                    f"slice stop {stop} reaches past the current instant "
                    f"({n} closed observations)"
                )
            return tuple(self._items[key])
        if key < 0:
            if -key > n:
                raise LookaheadError(
                    f"index {key} reaches before the start of history ({n} available)"
                )
            return self._items[key]
        if key >= n:
            raise LookaheadError(
                f"index {key} is at or beyond the current instant "
                f"({n} closed observations; the newest readable is {n - 1})"
            )
        return self._items[key]

    def last(self, k: int = 1) -> tuple[T, ...]:
        """The k most recent closed observations, oldest first. Short history
        returns what exists rather than raising -- a warmup shortfall is a
        legitimate state, unlike a reach forward.

        A tuple, like every slice of this view: nothing a strategy is handed
        should be mutable, and a returned list invites the habit even when the
        particular list is a harmless copy."""
        if k <= 0:
            return ()
        return tuple(self._items[-k:])

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"HistoryView(n={len(self._items)})"


class BarHistory:
    """Per-subscription `HistoryView`s, addressed by subscription id."""

    __slots__ = ("_by_sub",)

    def __init__(self, sub_ids: Sequence[str]):
        self._by_sub: dict[str, HistoryView[Bar]] = {s: HistoryView() for s in sub_ids}

    def __getitem__(self, sub_id: str) -> HistoryView[Bar]:
        try:
            return self._by_sub[sub_id]
        except KeyError:
            raise KeyError(
                f"no subscription {sub_id!r}; declare it in setup() "
                f"(have: {sorted(self._by_sub)})"
            ) from None

    def _append(self, bar: Bar) -> None:
        self._by_sub[bar.sub_id]._append(bar)

    def sub_ids(self) -> list[str]:
        return sorted(self._by_sub)
