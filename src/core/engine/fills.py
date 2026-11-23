"""Trade-based fill models, and the reason the loop scores on the pessimistic one.

Whichever model the agent is scored against is the one it learns to exploit.
Optimizing under `favorable` and checking under `unfavorable` systematically
selects for strategies whose edge lives in the optimism, so the loop scores on
`unfavorable` and `favorable` is diagnostic. The spread between them is itself
informative: a candidate that collapses between the two modes is telling you
where its edge comes from.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from ..types import (
    DataCapabilities,
    Fill,
    Order,
    OrderType,
    Quote,
    Side,
    Tick,
    Ts,
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


# Displayed top of book. Not a full book: an order behind the touch has no
# knowable queue under this capability, and the model says so by not joining it.
QUOTES_AND_TRADES = DataCapabilities(has_trades=True, has_quotes=True, has_book=False)

_PRICE_TOL = 1e-9


def _same(a: float, b: float) -> bool:
    return math.isclose(a, b, abs_tol=_PRICE_TOL)


class _QueueFill:
    """A resting limit joins the back of the displayed size and waits.

    Market, stop and stop-limit orders delegate to `unfavorable`. The queue is
    a claim about a limit that rested; a stop that triggers is taking, and
    pretending otherwise would be the optimistic fill this model exists to
    refuse.

    A trade strictly through the limit fills it immediately, at the limit. That
    is the `unfavorable` rule and it stays a sufficient condition: a print
    through the price traded past everyone at it.

    Cancels are the part a top-of-book feed cannot see. `credit_cancels` is the
    whole difference between the two models. The scored one does not credit
    them. See `docs/design/market-making.md`.
    """

    name = "queue"
    requires = QUOTES_AND_TRADES
    credit_cancels = False

    def __init__(self) -> None:
        self._bid: float | None = None
        self._ask: float | None = None
        self._bid_sz = 0
        self._ask_sz = 0
        self._traded_bid = 0
        self._traded_ask = 0
        # The price an order joined at. A move away from it drops the queue;
        # coming back joins the back again rather than resuming a place the
        # feed can no longer support.
        self._joined_at: dict[int, float] = {}
        self._through = UnfavorableFill()

    def observe(self, quote: Quote, orders: list[Order]) -> None:
        """A new top of book. Cancels are applied before the book is replaced."""
        if self.credit_cancels:
            self._credit(quote, orders)
        self._traded_bid = 0
        self._traded_ask = 0
        self._bid, self._ask = quote.bid_price, quote.ask_price
        self._bid_sz, self._ask_sz = quote.bid_size, quote.ask_size

    def _credit(self, quote: Quote, orders: list[Order]) -> None:
        if self._bid is not None and _same(quote.bid_price, self._bid) and self._bid_sz > 0:
            cancel = self._bid_sz - quote.bid_size - self._traded_bid
            if cancel > 0:
                keep = max(0.0, self._bid_sz - cancel) / self._bid_sz
                for order in orders:
                    if (order.side is Side.BUY and order.queue_ahead is not None
                            and order.limit_price is not None
                            and _same(order.limit_price, self._bid)):
                        order.queue_ahead *= keep
        if self._ask is not None and _same(quote.ask_price, self._ask) and self._ask_sz > 0:
            cancel = self._ask_sz - quote.ask_size - self._traded_ask
            if cancel > 0:
                keep = max(0.0, self._ask_sz - cancel) / self._ask_sz
                for order in orders:
                    if (order.side is Side.SELL and order.queue_ahead is not None
                            and order.limit_price is not None
                            and _same(order.limit_price, self._ask)):
                        order.queue_ahead *= keep

    def sync(self, order: Order) -> None:
        """Join the back, or leave, according to where the limit sits now."""
        if order.order_type is not OrderType.LIMIT or order.limit_price is None:
            return
        size = self._join_size(order)
        if size is None:
            order.queue_ahead = None
            self._joined_at.pop(order.order_id, None)
            return
        joined = self._joined_at.get(order.order_id)
        if joined is not None and _same(joined, order.limit_price) and order.queue_ahead is not None:
            return
        order.queue_ahead = size
        self._joined_at[order.order_id] = order.limit_price

    def _join_size(self, order: Order) -> float | None:
        limit = order.limit_price
        if limit is None or self._bid is None or self._ask is None:
            return None
        if order.side is Side.BUY:
            if limit > self._bid + _PRICE_TOL:
                return 0.0
            if _same(limit, self._bid):
                return float(self._bid_sz)
            return None
        if limit < self._ask - _PRICE_TOL:
            return 0.0
        if _same(limit, self._ask):
            return float(self._ask_sz)
        return None

    def note_trade(self, tick: Tick) -> None:
        """Trade volume at the current touch, so the next quote's size drop is
        not also counted as a cancel."""
        if self._bid is not None and _same(tick.price, self._bid):
            self._traded_bid += tick.size
        if self._ask is not None and _same(tick.price, self._ask):
            self._traded_ask += tick.size

    def resolve(self, order: Order, tick: Tick) -> Fill | None:
        if order.order_type is not OrderType.LIMIT:
            return self._through.resolve(order, tick)
        if order.limit_price is None:
            return None
        # Strictly through: the unfavorable rule, and it does not wait its turn.
        if self._through._crosses(order.side, order.limit_price, tick.price):
            size = min(order.remaining, tick.size)
            return Fill(price=order.limit_price, size=size) if size > 0 else None
        if order.queue_ahead is None or not self._hits(order, tick):
            return None
        ahead = order.queue_ahead
        if tick.size <= ahead + _PRICE_TOL:
            order.queue_ahead = ahead - tick.size
            return None
        got = min(order.remaining, int(math.floor(tick.size - ahead + _PRICE_TOL)))
        if got <= 0:
            order.queue_ahead = ahead - tick.size
            return None
        order.queue_ahead = 0.0
        self._joined_at.pop(order.order_id, None)
        return Fill(price=order.limit_price, size=got)

    def _hits(self, order: Order, tick: Tick) -> bool:
        """An aggressor traded at our price, on the side that reaches us.

        Unknown aggressor does not. A print at our price might be the other
        side of the spread, and counting it would fill us on a trade that did
        not touch our queue.
        """
        if order.limit_price is None or not _same(tick.price, order.limit_price):
            return False
        if order.side is Side.BUY:
            return tick.side is Side.SELL
        return tick.side is Side.BUY


class QueuePessimisticFill(_QueueFill):
    """Cancels do not move the queue. The model a quote-based run is scored on."""

    name = "queue-pessimistic"
    credit_cancels = False


class QueueProrataFill(_QueueFill):
    """A displayed-size drop with no trade is a cancel, shared pro rata.

    Diagnostic. A candidate whose edge disappears under `queue-pessimistic` and
    survives here was living in the cancel assumption.
    """

    name = "queue-prorata"
    credit_cancels = True


# Amendment 20. A quote older than this cannot price a fill or place a queue.
MAX_QUOTE_AGE_NS = 1_000_000_000
BOOK_AND_TRADES = DataCapabilities(has_trades=True, has_quotes=False, has_book=True)


@dataclass(slots=True)
class _Resting:
    """Where a resting limit stands in the book, as of `row` in `session`."""

    session: int
    row: int
    queue: float | None


class BookQueueFill:
    """Fills against one venue's real book, on the fast trade-driven walk (Amendment 20).

    * **Taker** orders (market, triggered stop, marketable limit) walk the visible
      levels at their arrival. An order larger than the five visible levels is refused.
    * **A resting limit** joins behind the displayed size. Only aggressor trades at its
      price move it forward, and cancels earn it nothing. A trade strictly through it
      fills it at the limit. If the best moves away, it rejoins at the back.
    * **Rows sharing a millisecond** have no knowable order, so a taker gets the worst.
    * **A quote older than `MAX_QUOTE_AGE_NS`** cannot price a taker or fill at the touch.

    Each fill records the mid, so the sign-flip null flips the mid-to-mid move and
    re-charges the spread and depth paid instead of turning them into a gain.
    """

    name = "book-queue-pessimistic"
    requires = BOOK_AND_TRADES

    def __init__(self) -> None:
        self._book_for = None
        self._resting: dict[int, _Resting] = {}
        self._rejected: dict[int, str] = {}

    def bind_book(self, book_for) -> None:
        self._book_for = book_for

    def needs_step(self, order: Order) -> bool:
        """A limit must be placed against the book at its first eligible tick."""
        if order.order_type is OrderType.STOP_LIMIT and not order.stop_armed:
            return False
        return (order.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT)
                and order.order_id not in self._resting)

    def take_rejection(self, order_id: int) -> str | None:
        return self._rejected.pop(order_id, None)

    def resolve(self, order: Order, tick: Tick) -> Fill | None:
        book = self._book_for(tick.session)
        if order.order_type is OrderType.MARKET:
            return self._take(order, tick, book, order.eligible_from_ts or tick.ts, None)
        if order.order_type is OrderType.STOP:
            if _stop_triggered(order, tick.price, strict=True):
                return self._take(order, tick, book, tick.ts, None)
            return None
        if order.order_type is OrderType.STOP_LIMIT and not order.stop_armed:
            if _stop_triggered(order, tick.price, strict=True):
                order.stop_armed = True
            return None
        if order.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT):
            return self._limit(order, tick, book)
        raise NotImplementedError(f"unhandled order type {order.order_type}")

    @staticmethod
    def _fresh_row(book, ts: Ts) -> int | None:
        row = book.row_at(ts)
        if row < 0 or ts - int(book.ts[row]) > MAX_QUOTE_AGE_NS:
            return None
        return row

    def _take(self, order: Order, tick: Tick, book, at: Ts, limit: float | None) -> Fill | None:
        row = self._fresh_row(book, at)
        if row is None and at != tick.ts:
            at, row = tick.ts, self._fresh_row(book, tick.ts)
        if row is None:
            return None
        # Every row sharing this millisecond, and the state just before them.
        first = int(np.searchsorted(book.ts, at, side="left"))
        rows = range(max(first - 1, 0), row + 1) if first <= row else (row,)
        worst = None
        for r in rows:
            got = self._walk(book, r, order.side, order.remaining, limit)
            if got is None:
                self._rejected[order.order_id] = (
                    f"{order.remaining} lots exceed the five visible book levels")
                return None
            size, notional = got
            if size <= 0:
                continue
            price = notional / size
            if (worst is None or (order.side is Side.BUY and price > worst[1])
                    or (order.side is Side.SELL and price < worst[1])):
                worst = (size, price, r)
        if worst is None:
            return None
        mid = (float(book.bid[worst[2]]) + float(book.ask[worst[2]])) / 2
        return Fill(price=worst[1], size=worst[0], liquidity="taker", mid=mid)

    @staticmethod
    def _walk(book, row: int, side: Side, size: int, limit: float | None):
        """(lots, notional) on the visible levels, or None if a market order cannot fit."""
        best = float(book.ask[row] if side is Side.BUY else book.bid[row])
        shown = int(book.ask_qty[row] if side is Side.BUY else book.bid_qty[row])
        usable = limit is None or (best <= limit + _PRICE_TOL if side is Side.BUY
                                   else best >= limit - _PRICE_TOL)
        if not usable:
            return 0, 0.0
        if size <= shown:
            return size, best * size
        prices, lots = book.levels(row, side)
        left, notional = size, 0.0
        for px, qty in zip(prices, lots):
            px, qty = float(px), int(qty)
            if qty <= 0:
                continue
            if limit is not None and (px > limit + _PRICE_TOL if side is Side.BUY
                                      else px < limit - _PRICE_TOL):
                break
            take = min(left, qty)
            notional += take * px
            left -= take
            if not left:
                break
        if left and limit is None:
            return None
        return size - left, notional

    def settle(self, side: Side, size: int, session: int, last_price: float) -> Fill:
        """The forced overnight close, against the session's last visible book."""
        book = self._book_for(session)
        row = len(book.ts) - 1
        got = self._walk(book, row, side, size, None) if row >= 0 else None
        if got is None:
            return Fill(price=last_price, size=size, liquidity="taker")
        mid = (float(book.bid[row]) + float(book.ask[row])) / 2
        return Fill(price=got[1] / got[0], size=size, liquidity="taker", mid=mid)

    def _limit(self, order: Order, tick: Tick, book) -> Fill | None:
        limit = order.limit_price
        if limit is None:
            return None
        state = self._resting.get(order.order_id)
        if state is None:
            at = order.eligible_from_ts or tick.ts
            row = self._fresh_row(book, at)
            if row is None and at != tick.ts:
                at, row = tick.ts, self._fresh_row(book, tick.ts)
            if row is None:
                return None
            crosses = (limit >= float(book.ask[row]) - _PRICE_TOL if order.side is Side.BUY
                       else limit <= float(book.bid[row]) + _PRICE_TOL)
            self._resting[order.order_id] = _Resting(tick.session, row, self._place(order, book, row))
            if crosses:
                got = self._take(order, tick, book, at, limit)
                self._rejected.pop(order.order_id, None)
                return got
            state = self._resting[order.order_id]
        row = book.row_at(tick.ts)
        self._advance(state, order, tick.session, book, row)
        mid = None if row < 0 else (float(book.bid[row]) + float(book.ask[row])) / 2
        through = (tick.price < limit - _PRICE_TOL if order.side is Side.BUY
                   else tick.price > limit + _PRICE_TOL)
        if through:
            size = min(order.remaining, tick.size)
            return Fill(price=limit, size=size, liquidity="maker", mid=mid) if size > 0 else None
        hits = (abs(tick.price - limit) <= _PRICE_TOL
                and tick.side is (Side.SELL if order.side is Side.BUY else Side.BUY))
        if not hits or state.queue is None or self._fresh_row(book, tick.ts) is None:
            return None
        if tick.size <= state.queue + _PRICE_TOL:
            state.queue -= tick.size
            return None
        got = min(order.remaining, int(math.floor(tick.size - state.queue + _PRICE_TOL)))
        state.queue = 0.0
        return Fill(price=limit, size=got, liquidity="maker", mid=mid) if got > 0 else None

    @staticmethod
    def _side_arrays(order: Order, book):
        return (book.bid, book.bid_qty) if order.side is Side.BUY else (book.ask, book.ask_qty)

    def _place(self, order: Order, book, row: int) -> float | None:
        px, qty = self._side_arrays(order, book)
        best, limit = float(px[row]), order.limit_price
        if abs(best - limit) <= _PRICE_TOL:
            return float(qty[row])
        improves = limit > best if order.side is Side.BUY else limit < best
        return 0.0 if improves else None

    def _advance(self, state: _Resting, order: Order, session: int, book, row: int) -> None:
        """Bring the queue forward from `state.row` to `row`, from the book alone."""
        if state.session != session:
            state.session, state.row, state.queue = session, -1, None
        if row <= state.row:
            return
        start = state.row + 1
        px, qty = self._side_arrays(order, book)
        seen = np.asarray(px[start:row + 1], dtype=float)
        limit = order.limit_price
        sign = 1.0 if order.side is Side.BUY else -1.0
        worse = sign * (seen - limit) > _PRICE_TOL        # the best is past us
        better = sign * (limit - seen) > _PRICE_TOL       # we are the best price
        queue = state.queue
        begin = 0
        if worse.any():
            last = int(np.flatnonzero(worse)[-1])
            queue, begin = None, last + 1
        if begin < seen.size:
            if better[begin:].any():
                queue = 0.0
            elif queue is None:
                queue = float(qty[start + begin])
        state.row, state.queue = row, queue


FILL_MODELS = {
    "favorable": FavorableFill,
    "unfavorable": UnfavorableFill,
    "queue-pessimistic": QueuePessimisticFill,
    "queue-prorata": QueueProrataFill,
    "book-queue-pessimistic": BookQueueFill,
}


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
