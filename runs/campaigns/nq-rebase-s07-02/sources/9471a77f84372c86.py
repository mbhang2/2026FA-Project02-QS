"""Opening range fade with three-outcome accounting, simplified for any session.

The question: when price leaves the opening range, does the move hold or does
the range win? This strategy fades the departure: it sells breakouts above the
range and buys breakdowns below, betting that the range wins. It explicitly
separates the three possible fates of a departure:

- "move holds": the protective stop is hit (price continues against the fade).
- "range wins": price returns inside the opening range and we close manually.
- "undecided": a time horizon expires with neither stop nor range return.

The opening range is the high/low of the first N bars of the session (default 6
five-minute bars = 30 minutes). This avoids dependence on a regular-hours open
offset that may not exist in synthetic or short sessions. A departure is defined
as a bar closing beyond the range by a threshold scaled by trailing realized
volatility (standard deviation of 5-minute close-to-close returns over the last
100 bars). The stop distance is also volatility-scaled. The time horizon for
undecided is 12 bars (1 hour) after entry.

The strategy acts on every session where a departure occurs, with no additional
filters. It is flat at session end by closing any open position 5 minutes before
the session close.
"""

from __future__ import annotations

import statistics
from collections import deque
from typing import Optional

# Opening range duration: number of bars at session start.
OPENING_RANGE_BARS = 6

# Volatility lookback: number of 5-minute bars for realized vol.
VOL_LOOKBACK = 100

# Departure threshold as multiple of realized volatility.
THRESHOLD_MULT = 0.5

# Stop distance as multiple of realized volatility.
STOP_MULT = 1.0

# Maximum holding period in bars before declaring undecided.
MAX_HOLD_BARS = 12

# Minimum volatility floor to avoid degenerate thresholds (index points).
MIN_VOL = 0.25

# Margin before session end to force flat (5 minutes in ns).
SESSION_END_MARGIN_NS = 5 * 60 * 1_000_000_000


class Strategy:
    """Fade opening range departures with volatility-scaled thresholds."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        opening_range_bars: int = OPENING_RANGE_BARS,
        vol_lookback: int = VOL_LOOKBACK,
        threshold_mult: float = THRESHOLD_MULT,
        stop_mult: float = STOP_MULT,
        max_hold_bars: int = MAX_HOLD_BARS,
        min_vol: float = MIN_VOL,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_range_bars = opening_range_bars
        self.vol_lookback = vol_lookback
        self.threshold_mult = threshold_mult
        self.stop_mult = stop_mult
        self.max_hold_bars = max_hold_bars
        self.min_vol = min_vol
        self.size = size

        # Per-session state
        self.session: Optional[int] = None
        self.range_high: Optional[float] = None
        self.range_low: Optional[float] = None
        self.range_established = False
        self.range_complete = False
        self.opening_range_bars_seen = 0
        self.traded_this_session = False

        # Volatility tracking
        self.returns_deque: deque[float] = deque(maxlen=vol_lookback)
        self.last_close: Optional[float] = None
        self.vol: Optional[float] = None

        # Trade state
        self.entry_direction = 0  # +1 long, -1 short, 0 flat
        self.entry_bar_count = 0
        self.entry_filled = False
        self.bar_count = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _reset_session(self, ctx) -> None:
        """Reset all per-session state at the start of a new session."""
        self.session = ctx.session
        self.range_high = None
        self.range_low = None
        self.range_established = False
        self.range_complete = False
        self.opening_range_bars_seen = 0
        self.traded_this_session = False
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

    def _check_departure(self, bar) -> bool:
        """Return True if a departure signal is present and we should trade."""
        if not self.range_established or not self.range_complete:
            return False
        if self.vol is None or self.range_high is None or self.range_low is None:
            return False
        threshold = self.threshold_mult * self.vol
        if bar.close > self.range_high + threshold:
            return True
        if bar.close < self.range_low - threshold:
            return True
        return False

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
        self.traded_this_session = True

    def _place_stop(self, ctx, entry_price: float) -> None:
        """Place a protective stop after entry fill."""
        if self.vol is None:
            return
        stop_distance = self.stop_mult * self.vol
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

        # Identify opening range bars (first N bars of session)
        if not self.range_complete:
            self.opening_range_bars_seen += 1
            if self.opening_range_bars_seen == 1:
                self.range_high = bar.high
                self.range_low = bar.low
            else:
                if self.range_high is not None:
                    self.range_high = max(self.range_high, bar.high)
                if self.range_low is not None:
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
                if self.range_low is not None and bar.close >= self.range_low:
                    self._close_position(ctx)
                    return
            else:  # short, fade breakout
                if self.range_high is not None and bar.close <= self.range_high:
                    self._close_position(ctx)
                    return
            # Check session end margin
            if ctx.ts > ctx.session_end - SESSION_END_MARGIN_NS:
                self._close_position(ctx)
                return

        # If flat, no working orders, and not already traded this session,
        # look for new departure
        if (
            ctx.position == 0
            and not ctx.working_order_ids
            and not self.traded_this_session
        ):
            if self._check_departure(bar):
                if self.vol is not None and self.range_high is not None:
                    if bar.close > self.range_high + self.threshold_mult * self.vol:
                        self._place_entry(ctx, -1)  # short
                    else:
                        self._place_entry(ctx, 1)   # long

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
