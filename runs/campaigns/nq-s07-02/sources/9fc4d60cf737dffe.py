"""Opening-range breakout on NQ 5m bars.

The question this campaign asks is whether a volatility-scaled departure from
the regular-hours opening range tends to hold (trend) or fail (mean-revert).
This strategy takes the trend side: it buys the first close above the opening
range plus a buffer and sells the first close below it minus the buffer.

Definitions, all explicit:
  * Opening range = high/low of 5m bars whose open_ts is between 09:30 and
    10:00 America/Chicago (30 minutes after the regular-hours open).
  * "Left" the range = bar close beyond the range by a buffer proportional to
    trailing realized volatility (EMA of absolute 5m closes).
  * Exit = bracket with stop 0.5 * range width, target 1.0 * range width.
  * Undecided = a position still open after 12 5m bars (one hour) is closed.
    The explicit time exit means time-outs do not silently count as either
    a range win or a move hold.

Trades at most once per session, and only during the first two hours of RTH.
The session close is flattened with a margin so positions do not ride into the
Globex overnight.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from statistics import median
from zoneinfo import ZoneInfo


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        range_minutes: int = 30,
        entry_window_minutes: int = 120,
        max_hold_bars: int = 12,
        size: int = 1,
        buffer_atr_mult: float = 0.5,
        stop_range_mult: float = 0.5,
        target_range_mult: float = 1.0,
        min_buffer: float = 2.5,
        min_stop: float = 5.0,
        min_target: float = 8.0,
        vol_alpha: float = 0.03,
    ) -> None:
        self.interval = interval
        self.range_minutes = range_minutes
        self.entry_window_minutes = entry_window_minutes
        self.max_hold_bars = max_hold_bars
        self.size = size
        self.buffer_atr_mult = buffer_atr_mult
        self.stop_range_mult = stop_range_mult
        self.target_range_mult = target_range_mult
        self.min_buffer = min_buffer
        self.min_stop = min_stop
        self.min_target = min_target
        self.vol_alpha = vol_alpha

        # Rolling volatility estimate. Initialised from the training split in
        # setup(), then updated incrementally from live 5m closes.
        self.vol_ema: float | None = None
        self.last_close: float | None = None

        # Session state.
        self.session: int | None = None
        self.rth_open_ns: int = 0
        self.rth_close_ns: int = 0
        self.range_high: float | None = None
        self.range_low: float | None = None
        self.range_formed: bool = False
        self.traded: bool = False

        # Open-position management.
        self.age: int = 0
        self.entry_session: int | None = None
        self._closing: bool = False

        # 15 minutes before RTH close, begin flattening.
        self.flatten_margin_ns = 15 * 60 * 1_000_000_000

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval, emit_empty=True)

        # Seed the volatility estimate from the training split: median absolute
        # 5m close-to-close move, computed within sessions.
        try:
            bars = ctx.train_bars(self.interval)
        except Exception:
            bars = ()

        returns: list[float] = []
        prev_close: float | None = None
        prev_session: int | None = None
        for bar in bars:
            if prev_close is not None and bar.tick_count > 0 and prev_session == bar.session:
                ret = abs(bar.close - prev_close)
                if ret > 0:
                    returns.append(ret)
            if bar.tick_count > 0:
                prev_close = bar.close
                prev_session = bar.session

        if returns:
            self.vol_ema = float(median(returns))

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return

        bar = ctx.bars[self.interval]

        if ctx.session != self.session:
            self._begin_session(ctx.session, ctx.session_start)

        self._update_volatility(bar)

        if ctx.position:
            self._manage_open(ctx)
            return

        if ctx.working_order_ids:
            return

        if not ctx.actionable:
            return

        if not self.range_formed:
            self._update_range(bar)
            if not self.range_formed:
                return

        self._maybe_enter(ctx, bar)

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        if fill.position == 0:
            self.age = 0
            self.entry_session = None
            self._closing = False
        else:
            self.age = 0
            self.entry_session = ctx.session
            self._closing = False

    def on_lifecycle(self, ctx) -> None:
        # No stored order ids to clean up; brackets and time exits are managed
        # through position state and tags.
        pass

    # ------------------------------------------------------------------
    # Session and timing helpers
    # ------------------------------------------------------------------
    def _begin_session(self, session: int, session_start_ns: int) -> None:
        self.session = session
        self.last_close = None
        self.rth_open_ns, self.rth_close_ns = self._rth_bounds(session_start_ns)
        self.range_high = None
        self.range_low = None
        self.range_formed = False
        self.traded = False
        self.age = 0
        self.entry_session = None
        self._closing = False

    def _rth_bounds(self, session_start_ns: int) -> tuple[int, int]:
        chicago = ZoneInfo("America/Chicago")
        dt = datetime.fromtimestamp(
            session_start_ns / 1_000_000_000, tz=timezone.utc
        ).astimezone(chicago)

        # The session opened at 17:00 CT on dt.date(), so regular-hours open is
        # 08:30 CT on the following calendar day.
        rth_date = dt.date() + timedelta(days=1)
        rth_open = datetime.combine(rth_date, time(8, 30), tzinfo=chicago)
        rth_close = datetime.combine(rth_date, time(15, 15), tzinfo=chicago)
        return (
            int(rth_open.timestamp() * 1_000_000_000),
            int(rth_close.timestamp() * 1_000_000_000),
        )

    # ------------------------------------------------------------------
    # Feature updates
    # ------------------------------------------------------------------
    def _update_volatility(self, bar) -> None:
        if bar.tick_count == 0:
            return

        if self.last_close is None:
            self.last_close = bar.close
            return

        ret = abs(bar.close - self.last_close)
        if ret > 0:
            if self.vol_ema is None:
                self.vol_ema = ret
            else:
                self.vol_ema = self.vol_alpha * ret + (1.0 - self.vol_alpha) * self.vol_ema

        self.last_close = bar.close

    def _update_range(self, bar) -> None:
        """Collect the opening range until the formation window closes."""
        t = bar.open_ts
        if t < self.rth_open_ns:
            return

        formation_end = self.rth_open_ns + self.range_minutes * 60 * 1_000_000_000
        if t < formation_end:
            if bar.tick_count == 0:
                return
            if self.range_high is None:
                self.range_high = bar.high
                self.range_low = bar.low
            else:
                self.range_high = max(self.range_high, bar.high)
                self.range_low = min(self.range_low, bar.low)
            return

        # First bar after the opening-range window: the range is now fixed.
        if self.range_high is not None and self.range_low is not None:
            self.range_formed = True

    # ------------------------------------------------------------------
    # Trade decisions
    # ------------------------------------------------------------------
    def _maybe_enter(self, ctx, bar) -> None:
        if self.traded or bar.tick_count == 0:
            return

        t = bar.open_ts
        if t < self.rth_open_ns + self.range_minutes * 60 * 1_000_000_000:
            return
        if t >= self.rth_open_ns + self.entry_window_minutes * 60 * 1_000_000_000:
            return

        if self.range_high is None or self.range_low is None:
            return
        range_width = self.range_high - self.range_low
        if range_width <= 0:
            return

        vol = self.vol_ema if self.vol_ema and self.vol_ema > 0 else max(range_width * 0.5, 1.0)
        buffer = max(self.buffer_atr_mult * vol, self.min_buffer)
        stop = max(self.stop_range_mult * range_width, self.min_stop)
        target = max(self.target_range_mult * range_width, self.min_target)

        if bar.close > self.range_high + buffer:
            oid = ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="opening-range-long",
            )
            if oid is not None:
                self.traded = True
        elif bar.close < self.range_low - buffer:
            oid = ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="opening-range-short",
            )
            if oid is not None:
                self.traded = True

    def _manage_open(self, ctx) -> None:
        if not ctx.actionable:
            return

        self.age += 1

        # Force flattening near the regular-hours close.
        if ctx.ts >= self.rth_close_ns - self.flatten_margin_ns:
            if not self._closing and ctx.position:
                ctx.close(tag="rth-close")
                self._closing = True
            return

        if self.age >= self.max_hold_bars:
            if not self._closing and ctx.position:
                ctx.close(tag="time-exit")
                self._closing = True
