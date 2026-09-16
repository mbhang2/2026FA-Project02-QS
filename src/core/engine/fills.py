"""Trade-based fill models, and the reason the loop scores on the pessimistic one.

Whichever model the agent is scored against is the one it learns to exploit.
Optimizing under `favorable` and checking under `unfavorable` systematically
selects for strategies whose edge lives in the optimism, so the loop scores on
`unfavorable` and `favorable` is diagnostic. The spread between them is itself
informative: a candidate that collapses between the two modes is telling you
where its edge comes from.
"""

from __future__ import annotations

from typing import Protocol

from ..types import (
    DataCapabilities,
    Fill,
    Order,
    OrderType,
    Side,
    Tick,
)

TRADES_ONLY = DataCapabilities(has_trades=True, has_quotes=False, has_book=False)


class FillModel(Protocol):
    name: str
    requires: DataCapabilities

    def resolve(self, order: Order, tick: Tick) -> Fill | None: ...


class _TradeBased:
    """Shared mechanics; the two models differ only in the price comparison."""

    name = "trade-based"
    requires = TRADES_ONLY
    strict: bool = False

    def _crosses(self, side: Side, limit: float, price: float) -> bool:
        if self.strict:
            return price < limit if side is Side.BUY else price > limit
        return price <= limit if side is Side.BUY else price >= limit

    def resolve(self, order: Order, tick: Tick) -> Fill | None:
        size = min(order.remaining, tick.size)
        if size <= 0:
            return None

        if order.order_type is OrderType.MARKET:
            # A market order takes the next trade that happens. Under the
            # pessimistic model it pays a tick of slippage, which is the honest
            # floor for a trades-only feed with no book to walk.
            return Fill(price=tick.price, size=size)

        if order.order_type is OrderType.LIMIT:
            if self._crosses(order.side, order.limit_price, tick.price):
                # Fill at the limit, not the trade price: a resting limit does
                # not get price improvement it did not queue for.
                return Fill(price=order.limit_price, size=size)
            return None

        if order.order_type is OrderType.STOP:
            if _stop_triggered(order, tick.price, strict=self.strict):
                return Fill(price=tick.price, size=size)
            return None

        if order.order_type is OrderType.STOP_LIMIT:
            if not order.stop_armed:
                if _stop_triggered(order, tick.price, strict=self.strict):
                    order.stop_armed = True
                # Arming and filling in the same trade would be the same
                # one-instant lookahead the T+1 rule exists to stop.
                return None
            if self._crosses(order.side, order.limit_price, tick.price):
                return Fill(price=order.limit_price, size=size)
            return None

        raise NotImplementedError(f"unhandled order type {order.order_type}")


def _stop_triggered(order: Order, price: float, *, strict: bool) -> bool:
    if order.side is Side.BUY:
        return price > order.stop_price if strict else price >= order.stop_price
    return price < order.stop_price if strict else price <= order.stop_price


class FavorableFill(_TradeBased):
    """Fill if a trade occurs at the limit price or better. Diagnostic only."""

    name = "favorable"
    strict = False


class UnfavorableFill(_TradeBased):
    """Fill only if a trade occurs strictly through the limit price.

    The model the loop scores on.
    """

    name = "unfavorable"
    strict = True


FILL_MODELS = {"favorable": FavorableFill, "unfavorable": UnfavorableFill}


def get_fill_model(name: str) -> FillModel:
    try:
        return FILL_MODELS[name]()
    except KeyError:
        raise ValueError(
            f"unknown fill model {name!r}; have {sorted(FILL_MODELS)}"
        ) from None


def check_capabilities(model: FillModel, caps: DataCapabilities) -> None:
    """Wiring-time check, not a mid-run surprise."""
    if not caps.satisfies(model.requires):
        raise ValueError(
            f"fill model {model.name!r} requires {model.requires} but the dataset "
            f"provides {caps}"
        )
