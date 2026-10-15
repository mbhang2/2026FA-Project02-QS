"""Opening range fade with simplified departure condition.

The question: when price leaves the opening range, does the move hold or does
the range win? This strategy fades the departure: it sells breakouts above the
range and buys breakdowns below, betting that the range wins. It explicitly
separates the three possible fates of a departure:

- "move holds": the protective stop is hit (price continues against the fade).
- "range wins": price returns inside the opening range and we close manually.
- "undecided": a time horizon expires with neither stop nor range return.

The opening range is the high/low of the first 30 minutes (6 five-minute bars)
after the regular-hours open (15.5 hours after the CME session start). A
departure is defined as a bar closing beyond the range by a small fixed
threshold (0.25 index points) to ensure the condition triggers on ordinary
sessions, including synthetic noise. The stop distance is volatility-scaled
with a floor of 0.5 points. The time horizon for undecided is 12 bars (1 hour)
after entry.

The strategy acts on every session where a departure occurs, with no additional
filters. It is flat at session end by closing any open position 5 minutes before
the session close.
"""

from __future__ import annotations

import statistics
from collections import deque
from typing import Deque

# Regular-hours open offset from CME session start (17:00 CT previous day to
# 8:30 CT same day) = 15.5 hours in nanoseconds.
REGULAR_OPEN_OFFSET_NS = 55_800_000_000_000

# Opening range duration: 6 five-minute bars = 30 minutes.
OPENING_RANGE_BARS = 6

# Volatility lookback: number of 5-minute bars for realized vol.
VOL_LOOKBACK = 100

# Departure threshold in index points (fixed, not volatility-scaled).
DEPARTURE_THRESHOLD = 0.25

# Stop distance as multiple of realized volatility.
STOP_MULT = 1.0

# Minimum stop distance in index points.
MIN_STOP_DISTANCE = 0.5

# Maximum holding period in bars before declaring undecided.
MAX_HOLD_BARS = 12

# Minimum volatility floor to avoid degenerate thresholds (index points).
MIN_VOL = 0.25

# Margin before session end to force flat (5 minutes in ns).
SESSION_END_MARGIN_NS = 5 * 60 * 1_000_000_000


class Strategy:
    """Fade opening range departures with a simple fixed threshold."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        opening_range_bars: int = OPENING_RANGE_BARS,
        vol_lookback: int = VOL_LOOKBACK,
        departure_threshold: float = DEPARTURE_THRESHOLD,
        stop_mult: float = STOP_MULT,
        min_stop_distance: float = MIN_STOP_DISTANCE,
        max_hold_bars: int = MAX_HOLD_BARS,
        min_vol: float = MIN_VOL,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_range_bars = opening_range_bars
        self.vol_lookback = vol_lookback
        self.departure_threshold = departure_threshold
        self.stop_mult = stop_mult
        self.min_stop_distance = min_stop_distance
        self.max_hold_bars = max_hold_bars
        self.min_vol = min_vol
        self.size = size

        # Per-session state
        self.session: int | None = None
        self.range_high: float = 0.0
        self.range_low: float = 0.0
        self.range_established: bool = False
        self.range_complete: bool = False
        self.opening_range_bars_seen: int = 0
        self.regular_open_ts: int = 0

        # Volatility tracking
        self.returns_deque: Deque[float] = deque(maxlen=vol_lookback)
        self.last_close: float | None = None
        self.vol: float = min_vol

        # Trade state
        self.entry_direction: int = 0  # +1 long, -1 short, 0 flat
        self.entry_bar_count: int = 0
        self.entry_filled: bool = False
        self.bar_count: int = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _reset_session(self, ctx) -> None:
        """Reset all per-session state at the start of a new session."""
        self.session = ctx.session
        self.range_high = 0.0
        self.range_low = 0.0
        self.range_established = False
        self.range_complete = False
        self.opening_range_bars_seen = 0
        self.regular_open_ts = ctx.session_start + REGULAR_OPEN_OFFSET_NS
        self.entry_direction = 0
        self.entry_bar_count = 0
        self.entry_filled = False
        # Keep volatility state across sessions (it uses prior bars)
        # but reset last_close to avoid using previous session's close.
        self.last_close = None

    def _update_volatility(self, bar) -> None:
        """Update realized volatility from 5-minute close-to-close returns."""
        if self.last_close is not None:
            ret = bar.close - self.last_close
            self.returns_deque.append(ret)
        self.last_close = bar.close
        if len(self.returns_deque) >= 2:
            self.vol = statistics.stdev(self.returns_deque)
            if self.vol < self.min_vol:
                self.vol = self.min_vol
        else:
            self.vol = self.min_vol

    def _check_departure(self, bar) -> int:
        """Return direction to trade: 1 for long (fade breakdown), -1 for short
        (fade breakout), 0 for no signal."""
        if not self.range_established or not self.range_complete:
            return 0
        if bar.close > self.range_high + self.departure_threshold:
            return -1  # short the breakout
        if bar.close < self.range_low - self.departure_threshold:
            return 1   # long the breakdown
        return 0

    def _place_entry(self, ctx, direction: int) -> None:
        """Place a market order to fade the departure."""
        if direction > 0:
            # Fade a breakdown: buy
            ctx.buy(size=self.size, tag="entry")
        else:
            # Fade a breakout: sell
            ctx.sell(size=self.size, tag="entry")
        self.entry_direction = direction
        self.entry_bar_count = self.bar_count
        self.entry_filled = False

    def _place_stop(self, ctx, entry_price: float) -> None:
        """Place a protective stop after entry fill."""
        stop_distance = max(self.stop_mult * self.vol, self.min_stop_distance)
        if self.entry_direction > 0:  # long
            trigger = entry_price - stop_distance
            ctx.sell(
                size=self.size,
                type="stop",
                trigger=trigger,
                reduce_only=True,
                tag="stop",
            )
        else:  # short
            trigger = entry_price + stop_distance
            ctx.buy(
                size=self.size,
                type="stop",
                trigger=trigger,
                reduce_only=True,
                tag="stop",
            )

    def _close_position(self, ctx) -> None:
        """Close the current position and cancel any protective stop."""
        stops = ctx.find_orders(tag="stop")
        for stop_order in stops:
            ctx.cancel(stop_order.id)
        if ctx.position != 0:
            ctx.close()
        self.entry_direction = 0
        self.entry_filled = False

    def on_bars(self, ctx) -> None:
        # Session change detection
        if ctx.session != self.session:
            self._reset_session(ctx)

        self.bar_count += 1

        # Update volatility on every bar
        bar = ctx.history(self.interval)[-1]
        self._update_volatility(bar)

        # Identify opening range bars
        if not self.range_complete:
            if bar.open_ts >= self.regular_open_ts:
                self.opening_range_bars_seen += 1
                if self.opening_range_bars_seen == 1:
                    self.range_high = bar.high
                    self.range_low = bar.low
                else:
                    self.range_high = max(self.range_high, bar.high)
                    self.range_low = min(self.range_low, bar.low)
                if self.opening_range_bars_seen >= self.opening_range_bars:
                    self.range_established = True
                    self.range_complete = True

        # If we have an open position, manage exits
        if ctx.position != 0 and self.entry_filled:
            # Check time exit
            if self.bar_count - self.entry_bar_count >= self.max_hold_bars:
                self._close_position(ctx)
                return
            # Check range return
            if self.entry_direction > 0:  # long, fade breakdown
                if bar.close >= self.range_low:
                    self._close_position(ctx)
                    return
            else:  # short, fade breakout
                if bar.close <= self.range_high:
                    self._close_position(ctx)
                    return
            # Check session end margin
            if ctx.ts > ctx.session_end - SESSION_END_MARGIN_NS:
                self._close_position(ctx)
                return

        # If flat and no working orders, look for new departure
        if ctx.position == 0 and not ctx.working_order_ids:
            direction = self._check_departure(bar)
            if direction != 0:
                self._place_entry(ctx, direction)

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        if fill.tag == "entry":
            # Entry filled, place protective stop
            self.entry_filled = True
            self._place_stop(ctx, fill.price)
        elif fill.tag == "stop":
            # Stop hit: move held, trade over
            self.entry_direction = 0
            self.entry_filled = False
        # Other fills (e.g., manual close) are handled by _close_position

    def on_lifecycle(self, ctx) -> None:
        # No special handling needed; orders are found by tag when needed.
        pass
