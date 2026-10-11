"""Opening-range breakout, one fill per session, with volatility-seeded sizing.

This strategy asks the campaign question in the "move holds" direction: when
price leaves the regular-hours opening range, it trades the departure.

Changes from prior completed build #0, which scored -0.0068 with 483 fills:

* One filled entry per session.  #0 could re-enter after a bracket leg filled,
  paying commission without a new signal.  This keeps the entry window and
  buffer from #0 so the strategy remains exposed in substantially all sessions,
  while removing the extra round trips.
* `emit_empty=True` on the 5m subscription so the time horizon can fire even
  when the late session has no trades.  Prior builds without this could miss
  their own exit when the tape went quiet.
* Daily volatility is seeded from `ctx.train_bars("1d")` in setup and then
  switched to live trailing values once 20 live sessions exist.  This makes
  early sessions tradable and later sessions adaptive.
* After price moves in our favor by a fraction of the initial stop distance,
  the protective stop is tightened to breakeven.  This is a risk rule, not a
  regime model: if the departure starts to hold and then retraces, we no longer
  give back the full stop loss; if it holds straight to target, the modification
  never fires.

Only one departure is acted on per session.  Undecided departures are closed by
the time horizon rather than being silently counted as range wins or move holds.
"""

from __future__ import annotations

from src.core.types import NS_PER_SECOND, OrderType


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        daily_interval: str = "1d",
        range_minutes: int = 30,
        rth_open_offset_seconds: int = 55800,   # 17:00 CT -> 08:30 CT
        rth_close_offset_seconds: int = 80100,  # 17:00 CT -> 15:15 CT
        trailing_days: int = 20,
        breakout_vol_mult: float = 0.04,
        stop_mult: float = 2.5,
        min_stop_frac: float = 0.5,
        reward_risk: float = 1.5,
        target_mult: float = 3.0,
        size: int = 1,
        max_hold_minutes: int = 75,
        entry_window_minutes: int = 120,
        breakeven_frac: float = 0.75,
    ):
        self.interval = interval
        self.daily_interval = daily_interval
        self.size = size
        self.trailing_days = trailing_days
        self.breakout_vol_mult = breakout_vol_mult
        self.stop_mult = stop_mult
        self.min_stop_frac = min_stop_frac
        self.reward_risk = reward_risk
        self.target_mult = target_mult
        self.breakeven_frac = breakeven_frac

        self.range_ns = range_minutes * 60 * NS_PER_SECOND
        self.entry_window_ns = entry_window_minutes * 60 * NS_PER_SECOND
        self.max_hold_ns = max_hold_minutes * 60 * NS_PER_SECOND
        self.rth_open_offset_ns = rth_open_offset_seconds * NS_PER_SECOND
        self.rth_close_offset_ns = rth_close_offset_seconds * NS_PER_SECOND

        self._session = None
        self._range_high = None
        self._range_low = None
        self._range_done = False
        self._daily_vol = None
        self._initial_daily_vol = None
        self._deadline_ns = None
        self._traded_this_session = False

        self._entry_price = None
        self._entry_side = 0
        self._entry_oid = None
        self._stop_off_at_entry = None
        self._stop_tightened = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval, emit_empty=True)
        ctx.subscribe(self.daily_interval)

        # Seed trailing daily volatility from the training split.  This is
        # training data only, so it carries no lookahead into scored sessions.
        self._initial_daily_vol = None
        try:
            daily_bars = ctx.train_bars(self.daily_interval)
            if daily_bars:
                recent = daily_bars[-self.trailing_days :]
                ranges = [b.high - b.low for b in recent if b.high > b.low]
                if ranges:
                    self._initial_daily_vol = sum(ranges) / len(ranges)
        except Exception:
            pass

    def _update_daily_vol(self, ctx) -> None:
        hist = ctx.history(self.daily_interval)
        if len(hist) < self.trailing_days:
            return
        days = hist.last(self.trailing_days)
        ranges = [b.high - b.low for b in days if b.high > b.low]
        if ranges:
            self._daily_vol = sum(ranges) / len(ranges)

    def _update_range(self, bar) -> None:
        if self._range_high is None or bar.high > self._range_high:
            self._range_high = bar.high
        if self._range_low is None or bar.low < self._range_low:
            self._range_low = bar.low

    def _tighten_stop_to_breakeven(self, ctx) -> None:
        if self._entry_oid is None:
            return
        legs = ctx.find_orders(parent_id=self._entry_oid)
        for order in legs:
            if order.order_type != OrderType.STOP:
                continue

            if self._entry_side > 0:
                new_trigger = self._entry_price + 0.25
                if new_trigger >= ctx.last_price:
                    new_trigger = ctx.last_price - 0.25
            else:
                new_trigger = self._entry_price - 0.25
                if new_trigger <= ctx.last_price:
                    new_trigger = ctx.last_price + 0.25

            ctx.modify(order.id, trigger=new_trigger)
            self._stop_tightened = True
            return

    def _manage_position(self, ctx) -> None:
        if self._deadline_ns is not None and ctx.ts >= self._deadline_ns:
            if ctx.working_order_ids:
                ctx.cancel_all()
            ctx.close()
            self._deadline_ns = None
            return

        # Once price has moved far enough in our favor, move the protective
        # stop to breakeven so a round trip that has already shown follow-through
        # does not become a full stop loss.
        if (
            not self._stop_tightened
            and self._entry_price is not None
            and self._entry_side != 0
            and self._stop_off_at_entry is not None
        ):
            distance = self.breakeven_frac * self._stop_off_at_entry
            if self._entry_side > 0:
                if ctx.last_price >= self._entry_price + distance:
                    self._tighten_stop_to_breakeven(ctx)
            else:
                if ctx.last_price <= self._entry_price - distance:
                    self._tighten_stop_to_breakeven(ctx)

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        if ctx.session != self._session:
            self._session = ctx.session
            self._range_high = None
            self._range_low = None
            self._range_done = False
            self._daily_vol = self._initial_daily_vol
            self._deadline_ns = None
            self._traded_this_session = False
            self._entry_price = None
            self._entry_side = 0
            self._entry_oid = None
            self._stop_off_at_entry = None
            self._stop_tightened = False

        # Always prefer live trailing volatility once enough daily bars exist.
        hist = ctx.history(self.daily_interval)
        if len(hist) >= self.trailing_days:
            self._update_daily_vol(ctx)

        rth_open = ctx.session_start + self.rth_open_offset_ns
        range_end = rth_open + self.range_ns

        if bar.close_ts <= rth_open:
            return

        # Accumulate the opening range.  Empty bars advance time but do not
        # alter the range.
        if bar.close_ts < range_end:
            if bar.tick_count > 0:
                self._update_range(bar)
            return
        if bar.close_ts == range_end:
            if bar.tick_count > 0:
                self._update_range(bar)
            self._range_done = True
            return
        if not self._range_done:
            self._range_done = True
            return

        if not ctx.actionable:
            return

        if ctx.position:
            self._manage_position(ctx)
            return

        if ctx.working_order_ids:
            return

        if bar.tick_count == 0:
            return

        # One filled entry per session.
        if self._traded_this_session:
            return

        if self._range_high is None or self._range_low is None:
            return
        if bar.close_ts > rth_open + self.entry_window_ns:
            return
        if self._daily_vol is None or self._daily_vol <= 0:
            return

        buffer = self.breakout_vol_mult * self._daily_vol
        side = 0
        if bar.close > self._range_high + buffer:
            side = 1
        elif bar.close < self._range_low - buffer:
            side = -1
        if side == 0:
            return

        range_width = max(self._range_high - self._range_low, 0.0)
        stop_off = max(self.stop_mult * buffer, self.min_stop_frac * range_width, 2.0)
        target_off = max(self.target_mult * buffer, stop_off * self.reward_risk)
        target_off = max(target_off, stop_off * 1.1)

        self._entry_side = side
        self._stop_off_at_entry = stop_off

        if side > 0:
            oid = ctx.buy(
                size=self.size,
                stop_offset=stop_off,
                target_offset=target_off,
                tag="orb-long",
            )
        else:
            oid = ctx.sell(
                size=self.size,
                stop_offset=stop_off,
                target_offset=target_off,
                tag="orb-short",
            )

        if oid is not None:
            self._entry_oid = oid
        else:
            self._entry_oid = None

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        if fill.position == 0:
            self._deadline_ns = None
            return

        self._traded_this_session = True

        if self._entry_price is None:
            self._entry_price = fill.price

        if self._deadline_ns is None:
            rth_close = ctx.session_start + self.rth_close_offset_ns
            deadline = ctx.ts + self.max_hold_ns
            hard_deadline = rth_close - 5 * 60 * NS_PER_SECOND
            if hard_deadline > ctx.ts and hard_deadline < deadline:
                deadline = hard_deadline
            self._deadline_ns = deadline

    def on_lifecycle(self, ctx) -> None:
        pass
