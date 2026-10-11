"""Opening range fade.

The question this campaign asks: when price leaves the opening range, does the
move hold or does the range win? This strategy answers "the range wins" and does
so in a way that trades the ordinary run of sessions rather than a handful.

Mechanics:
- Use 5m bars, flagged empty if no ticks; empty bars are ignored for signals but
  kept so we stay aligned to the session grid.
- The regular-hours open is taken as 8:30 America/Chicago, which is 15.5 hours
  after the CME Globex session open (17:00 CT the prior day). The opening range
  is the high and low of the first 30 minutes of regular trading (8:30-9:00 CT).
- At the first bar that closes after the range has formed, if price is above the
  opening high by a small buffer, we fade it: sell short. If price is below the
  opening low, we fade it: buy long.
- The bracket is sized off the opening range height: stop = 0.5 * range, target
  = 1.0 * range, with sensible floors and caps. This gives approximately 2:1
  reward-to-risk when the range is normal-sized.
- We attempt only one entry per session and force flat 30 minutes before the
  session close so the result measures the intraday opening-range effect, not an
  overnight carry.
"""

from __future__ import annotations
from typing import Optional


class Strategy:
    """Fade regular-hours opening-range breakouts."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        opening_range_minutes: int = 30,
        rth_open_hours_after_session_start: float = 15.5,
        breakout_buffer: float = 1.0,
        stop_mult: float = 0.5,
        target_mult: float = 1.0,
        min_stop: float = 2.0,
        max_stop: float = 50.0,
        min_target: float = 2.0,
        max_target: float = 150.0,
        exit_margin_minutes: int = 30,
        size: int = 1,
    ):
        self.interval = interval
        self.size = size

        NS = 1_000_000_000
        self._rth_open_offset_ns: int = int(
            rth_open_hours_after_session_start * 60 * 60 * NS
        )
        self._opening_range_ns: int = int(opening_range_minutes * 60 * NS)
        self._exit_margin_ns: int = int(exit_margin_minutes * 60 * NS)

        self._breakout_buffer = breakout_buffer
        self._stop_mult = stop_mult
        self._target_mult = target_mult
        self._min_stop = min_stop
        self._max_stop = max_stop
        self._min_target = min_target
        self._max_target = max_target

        # Timestamps are initialized to 0 rather than None so mypy sees int.
        # `_reset_session` sets them before any use.
        self._session: int = -1
        self._regular_open_ts: int = 0
        self._opening_end_ts: int = 0
        self._exit_ts: int = 0
        self._range_high: Optional[float] = None
        self._range_low: Optional[float] = None
        self._range_ready: bool = False
        self._traded_this_session: bool = False

    def setup(self, ctx) -> None:
        # Empty intervals are useful so we still get a callback at regular time
        # boundaries even if there is no trade; they are filtered out before any
        # decision.
        ctx.subscribe(self.interval, emit_empty=True)

    def _reset_session(self, ctx) -> None:
        self._session = ctx.session
        self._regular_open_ts = ctx.session_start + self._rth_open_offset_ns
        self._opening_end_ts = self._regular_open_ts + self._opening_range_ns
        self._exit_ts = ctx.session_end - self._exit_margin_ns
        self._range_high = None
        self._range_low = None
        self._range_ready = False
        self._traded_this_session = False

    def on_bars(self, ctx) -> None:
        bar = ctx.bars.get(self.interval)
        if bar is None:
            return

        if ctx.session != self._session:
            self._reset_session(ctx)

        # Empty bars carry previous close forward and do not represent actual
        # price discovery; skip them for range and signal.
        if bar.tick_count == 0:
            return

        # Build the opening range.
        if not self._range_ready:
            if (
                bar.close_ts > self._regular_open_ts
                and bar.close_ts <= self._opening_end_ts
            ):
                if self._range_high is None:
                    self._range_high = bar.high
                    self._range_low = bar.low
                else:
                    self._range_high = max(self._range_high, bar.high)
                    self._range_low = min(self._range_low, bar.low)
                return
            elif bar.close_ts > self._opening_end_ts:
                self._range_ready = True
                if self._range_high is None or self._range_low is None:
                    return
                # Fall through: this is the first post-range bar and may already
                # be a breakout.
            else:
                return

        if self._range_high is None or self._range_low is None:
            return

        # Narrow for mypy and keep the rest of the logic clear.
        range_high = self._range_high
        range_low = self._range_low

        if not ctx.actionable:
            return

        # Force flat before the session close so we are measuring the
        # regular-hours opening-range trade, not an overnight position.
        if ctx.position and ctx.ts >= self._exit_ts:
            ctx.cancel_all()
            ctx.close()
            return

        # One position at a time, and one entry attempt per session.
        if ctx.position or ctx.working_order_ids:
            return
        if self._traded_this_session:
            return
        if ctx.ts >= self._exit_ts:
            return

        range_height = range_high - range_low
        if range_height <= self._min_stop:
            return

        stop = max(self._min_stop, min(self._max_stop, self._stop_mult * range_height))
        target = max(
            self._min_target, min(self._max_target, self._target_mult * range_height)
        )

        # Fade the breakout: short above opening high, long below opening low.
        if bar.close > range_high + self._breakout_buffer:
            oid = ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="opening-range-fade-short",
            )
            if oid is not None:
                self._traded_this_session = True
        elif bar.close < range_low - self._breakout_buffer:
            oid = ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="opening-range-fade-long",
            )
            if oid is not None:
                self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
