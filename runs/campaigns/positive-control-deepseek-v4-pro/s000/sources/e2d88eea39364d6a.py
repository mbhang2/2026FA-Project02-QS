"""Long-side trend following with a trailing ATR stop, re-entering each session.

Prior trials show a single continuous long position produced the best score so
far, and short-biased exposure produced worse scores.  This strategy keeps the
long bias but adds a wide, volatility-scaled trailing stop to cut the left tail
without the high turnover of earlier momentum attempts.

The position is entered at the first actionable instant of every session when
flat, using a GTC market order and a protective stop bracket.  The stop is then
trailed upward on 5-minute bar closes, never downward, using a 3x ATR distance.
No profit target is used, allowing the positive drift to run.  If the stop is
hit, the strategy waits until the next session_open to re-enter, which keeps
exposure in every session while avoiding intra-session whipsaw.
"""

from __future__ import annotations

from src.core.types import Side


class Strategy:
    """GTC long with a trailing, ATR-based protective stop."""

    def __init__(self, *, interval: str = "5m", size: int = 1,
                 atr_period: int = 14, atr_mult: float = 3.0,
                 min_stop: float = 0.75):
        self.interval = interval
        self.size = size
        self.atr_period = atr_period
        self.atr_mult = atr_mult
        self.min_stop = min_stop

        # Rolling bar data kept on self so we never rely on the full history
        # window.  Each bar is stored as (high, low, close).
        self._bars: list[tuple[float, float, float]] = []
        self._atr: float = 1.0

        self._entry_id: int | None = None
        self._highest_close: float | None = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _stop_distance(self, ctx) -> float:
        """A volatility-scaled stop distance, guarded for price scale."""
        distance = max(self.atr_mult * self._atr, self.min_stop)
        if ctx.last_price and ctx.last_price > 0:
            # Make the stop at least a tiny fraction of price, but never so
            # large that a stop is placed at or below zero.
            distance = max(distance, 0.0005 * ctx.last_price)
            distance = min(distance, 0.5 * ctx.last_price)
        return distance

    def _append_bar(self, ctx) -> None:
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        bar = history[-1]
        self._bars.append((bar.high, bar.low, bar.close))
        if len(self._bars) > 200:
            self._bars.pop(0)
        self._update_atr()

    def _update_atr(self) -> None:
        if len(self._bars) < 2:
            self._atr = 1.0
            return

        true_ranges: list[float] = []
        for i in range(1, len(self._bars)):
            prev_close = self._bars[i - 1][2]
            high, low, close = self._bars[i]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            true_ranges.append(tr)

        sample = true_ranges[-self.atr_period:]
        if sample:
            self._atr = sum(sample) / len(sample)
        else:
            self._atr = 1.0
        if self._atr <= 0:
            self._atr = 1.0

    def _enter(self, ctx) -> None:
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        stop_offset = self._stop_distance(ctx)
        order_id = ctx.buy(
            size=self.size,
            tif="GTC",
            stop_offset=stop_offset,
            tag="long-trend",
        )
        if order_id is not None:
            self._entry_id = order_id
            self._highest_close = None

    def on_bars(self, ctx) -> None:
        self._append_bar(ctx)

        if self._entry_id is None or ctx.position <= 0:
            return

        if self._highest_close is None:
            self._highest_close = self._bars[-1][0] if self._bars else ctx.last_price
        else:
            self._highest_close = max(self._highest_close, self._bars[-1][0])

        legs = ctx.find_orders(parent_id=self._entry_id)
        stop_orders = [o for o in legs if o.side == Side.SELL]
        if not stop_orders:
            return

        stop = stop_orders[0]
        desired_stop = self._highest_close - self._stop_distance(ctx)
        # Never place a sell stop at or above the market; keep it just below.
        desired_stop = min(desired_stop, ctx.last_price - 0.01)

        # Only raise the stop, never lower it.
        if desired_stop > stop.stop_price:
            ctx.modify(stop.id, trigger=desired_stop)

    def on_fill(self, ctx) -> None:
        fill = ctx.fill

        if fill.side == Side.BUY and fill.position > 0:
            # Entry fill: initalise the trailing high.
            self._highest_close = fill.price
        elif fill.side == Side.SELL and fill.position == 0:
            # Protective stop filled; wait for the next session_open to re-enter.
            self._entry_id = None
            self._highest_close = None

    def on_lifecycle(self, ctx) -> None:
        if ctx.event.kind == "session_open":
            self._enter(ctx)
