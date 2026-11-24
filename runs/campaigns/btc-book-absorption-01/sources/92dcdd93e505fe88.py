"""Absorption fade on the OKX BTC-USDT perpetual: aggressive flow that makes no progress.

The claim under test is unchanged and so is the direction of the trade: after a
directional move, aggressive flow that runs into displayed liquidity which
*holds* marks a turn, so we short absorbed buying after a rise and buy absorbed
selling after a fall. Never with the flow.

Four attempts so far, all negative: -0.293 / -0.105 / -0.104 / -0.128. Reading
the per-trade arithmetic backwards tells us where the money went, and it is
worth writing down because it drives every choice here.

  * Attempt #1 (market entry, stop 35bp / target 50bp, 6,587 trades) lost
    -0.105. Backing out the hit rate: 87p - 39 = -1.6bp per trade gives p = 0.43
    against a 41.2% price-only break-even. So the *price* legs of that version
    were mildly positive and the entry fee ate the whole of it.
  * Attempt #2 (maker entry at the mid, stop 30bp / target 40bp, 3,054 trades)
    lost -0.104. Backing out: 72p - 32 = -3.4bp gives p = 0.397 against a 42.9%
    break-even. The maker entry saved 2bp of fee and cost more than that in hit
    rate -- a resting bid at the mid fills only when price first comes to it,
    i.e. on exactly the signals that are still going the wrong way. That is
    adverse selection and it is bigger than the fee it saves.
  * Attempt #0 (target 25bp) lost -0.293 and attempt #3 (signal richer, market
    entry, tighter) lost -0.128. Small brackets lose; the 25bp version lost
    three times as much as the 50bp one at the same trade count.

So the ranking is: tight brackets lose badly, the hit rate is roughly a coin
flip once you pay for the entry, and the difference between the maker and taker
variants is inside the noise. What is left to change is the *signal*, not the
fee.

What this version changes:

  1. **Absorption is measured as flow without progress, not as a stall
     heuristic.** The claim is literally "aggressive buying is unusually large
     and the best ask does not move up". So over the recent window we require
     one-sided aggressive flow at |delta|/volume >= 0.20 *and* a price change of
     no more than 30% of the prior move in the flow's direction, *and* no new
     extreme: for a fade of a rise, the highest high of the recent window must
     not exceed the highest high of the prior window by more than 5bp. That
     last test is the direct statement "the offer did not lift" and prior
     versions never made it -- they only checked that the *close* had stalled,
     which a wick through the offer satisfies.
  2. **A structural stop instead of a constant one.** The stop sits just beyond
     the extreme the flow failed to break, plus a 10bp buffer, clamped to
     [25bp, 90bp]. If the absorbed high is taken out, the premise is wrong and
     there is nothing left to wait for. A 30bp constant stop is simultaneously
     too wide for a 15bp failed push and too tight for a 90bp one.
  3. **A retrace target, floored so costs cannot dominate.** Target is 40% of
     the prior move, floored at 35bp and capped at 120bp. That keeps the
     bracket at least ten times the 3-4bp round trip, which is the mistake
     attempt #0 made at 25bp.
  4. **Market entry, deliberately.** Attempt #2 proved the maker-at-mid entry
     selects the losers. Paying 2bp for an unbiased sample of the signal is
     worth more than saving it, because the entire question is whether the
     signal has an edge at all.
  5. **Bigger prior moves.** 60bp over 25 one-minute bars, evaluated after an
     8-bar stall window. On BTC that is roughly a 1.7-sigma half-hour move, so
     it occurs several times a day and the strategy still trades most days; a
     25bp move is inside one sigma and fading it is fading noise.

Book handling: `ctx.book()` returns one row and no history, so the top-5 size on
each side is sampled once per bar onto `self` and compared against its own
rolling median over the last 40 samples. The absorbing side must be at or above
its own median (a wall, not a thin book) and must not have fallen below 80% of
what it was when the stall window opened (it was not eaten by the flow it was
absorbing). Both are ratios within one series, so a change of lot convention
cannot quietly rescale them. Missing or stale rows are skipped rather than
recorded as zero, so a gap in the feed does not become a collapse in depth.

Everything else is the housekeeping the previous files already established:
1m bars with emit_empty so the pre-close flatten always has a callback, at most
4 entries a session at least 20 bars apart, a 60-bar time stop, and a hard
flatten with everything cancelled 5 minutes before the UTC day turns so that
every trade opens from flat and closes to flat inside its own day and no
position is ever carried across a funding settlement. Position, working orders,
actionable flag and remaining session time are checked before every order, and
`None` from buy/sell is read as "no order exists" rather than assumed away.
"""

from __future__ import annotations

from collections import deque
from statistics import median

NS_PER_SECOND = 1_000_000_000


class Strategy:
    """Fade aggressive flow that fails to move price against the visible book."""

    def __init__(
        self,
        interval: str = "1m",
        size: int = 100,
        move_bars: int = 25,
        stall_bars: int = 8,
        min_move: float = 0.0060,
        stall_cap: float = 0.30,
        giveback_cap: float = 0.50,
        new_extreme_tol: float = 0.0005,
        min_flow: float = 0.20,
        depth_ratio: float = 1.05,
        refill_ratio: float = 0.80,
        depth_window: int = 40,
        depth_min: int = 16,
        retrace: float = 0.40,
        min_target: float = 0.0035,
        max_target: float = 0.0120,
        stop_buffer: float = 0.0010,
        min_stop: float = 0.0025,
        max_stop: float = 0.0090,
        max_hold_bars: int = 60,
        max_entries: int = 4,
        max_attempts: int = 8,
        min_spacing_bars: int = 20,
        max_spread_frac: float = 0.0008,
        flat_margin_s: int = 300,
        min_time_to_close_s: int = 1800,
    ) -> None:
        self.interval = interval
        self.size = max(1, int(size))
        self.move_bars = max(5, int(move_bars))
        self.stall_bars = max(2, int(stall_bars))
        self.min_move = float(min_move)
        self.stall_cap = float(stall_cap)
        self.giveback_cap = float(giveback_cap)
        self.new_extreme_tol = float(new_extreme_tol)
        self.min_flow = float(min_flow)
        self.depth_ratio = float(depth_ratio)
        self.refill_ratio = float(refill_ratio)
        self.depth_window = max(8, int(depth_window))
        self.depth_min = max(4, int(depth_min))
        self.retrace = float(retrace)
        self.min_target = float(min_target)
        self.max_target = float(max_target)
        self.stop_buffer = float(stop_buffer)
        self.min_stop = float(min_stop)
        self.max_stop = float(max_stop)
        self.max_hold_bars = max(1, int(max_hold_bars))
        self.max_entries = max(1, int(max_entries))
        self.max_attempts = max(1, int(max_attempts))
        self.min_spacing_bars = max(0, int(min_spacing_bars))
        self.max_spread_frac = float(max_spread_frac)
        self.flat_margin_s = int(flat_margin_s)
        self.min_time_to_close_s = int(min_time_to_close_s)

        # Rolling state. ctx.history() is a bounded window and the book has no
        # history at all, so every series compared over time is kept here.
        cap = self.move_bars + self.stall_bars + 16
        self._closes: deque[float] = deque(maxlen=cap)
        self._highs: deque[float] = deque(maxlen=cap)
        self._lows: deque[float] = deque(maxlen=cap)
        self._deltas: deque[float] = deque(maxlen=cap)
        self._volumes: deque[float] = deque(maxlen=cap)

        book_cap = self.depth_window + 8
        self._ask_depth: deque[float] = deque(maxlen=book_cap)
        self._bid_depth: deque[float] = deque(maxlen=book_cap)

        self._session: int | None = None
        self._entries = 0
        self._attempts = 0
        self._since_entry = 10 ** 9
        self._hold_bars = 0

    # ------------------------------------------------------------------ wiring

    def setup(self, ctx) -> None:
        # emit_empty so the pre-close flatten always has a callback to fire on:
        # a quiet stretch before midnight must not strand a position overnight.
        ctx.subscribe(self.interval, emit_empty=True)
        # No callback of its own; this only makes ctx.book() readable.
        ctx.subscribe("book")

    # -------------------------------------------------------------------- bars

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        if ctx.session != self._session:
            self._session = ctx.session
            self._entries = 0
            self._attempts = 0
            self._since_entry = 10 ** 9
            self._hold_bars = 0

        if self._since_entry < 10 ** 9:
            self._since_entry += 1

        book = self._read_book(ctx)
        if book is not None:
            ask, bid, ask_depth, bid_depth = book
            # Only real rows go into the history: a missing or stale row is not
            # a collapse in displayed size, and recording it as one would make
            # every feed gap look like the book being eaten.
            self._ask_depth.append(ask_depth)
            self._bid_depth.append(bid_depth)
        else:
            ask = 0.0
            bid = 0.0

        # Empty bars carry the previous close forward at zero volume. They
        # advance the clocks but they are not evidence about price or flow.
        if bar.tick_count > 0:
            self._closes.append(float(bar.close))
            self._highs.append(float(bar.high))
            self._lows.append(float(bar.low))
            self._deltas.append(float(bar.delta))
            self._volumes.append(float(bar.volume))

        remaining = ctx.session_end - ctx.ts
        if remaining <= self.flat_margin_s * NS_PER_SECOND:
            self._flatten(ctx)
            return

        if not ctx.actionable:
            return

        # An open position: the bracket owns the price exits and was armed at
        # the instant of the fill. Only the time stop is left to manage.
        if ctx.position != 0:
            self._hold_bars += 1
            if self._hold_bars > self.max_hold_bars:
                self._flatten(ctx)
            return
        self._hold_bars = 0

        if ctx.working_order_ids:
            return
        if self._entries >= self.max_entries:
            return
        if self._attempts >= self.max_attempts:
            return
        if self._since_entry < self.min_spacing_bars:
            return
        if remaining < self.min_time_to_close_s * NS_PER_SECOND:
            return
        if book is None:
            return
        if ask <= 0.0 or bid <= 0.0 or ask <= bid:
            return

        midpoint = 0.5 * (ask + bid)
        if midpoint <= 0.0:
            return
        if (ask - bid) > self.max_spread_frac * midpoint:
            return
        price = float(ctx.last_price)
        if price <= 0.0:
            return
        if abs(midpoint - price) > 0.01 * price:
            return

        signal = self._signal()
        if signal is None:
            return
        direction, move_abs, extreme = signal

        # Structural stop: just past the extreme the aggressive flow failed to
        # take out, plus a buffer, clamped so it can never be a hair-trigger or
        # a hold-for-the-day.
        if direction < 0:
            raw_stop = extreme - price
        else:
            raw_stop = price - extreme
        stop_dist = raw_stop + self.stop_buffer * price
        stop_dist = min(max(stop_dist, self.min_stop * price),
                        self.max_stop * price)

        # Retrace target: 40% of the move that is being faded, floored so the
        # bracket stays large against a 3-4bp round trip.
        target_dist = self.retrace * move_abs * price
        target_dist = min(max(target_dist, self.min_target * price),
                          self.max_target * price)

        if stop_dist <= 0.0 or target_dist <= 0.0:
            return

        if direction > 0:
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="absorb-long",
            )
        else:
            order_id = ctx.sell(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="absorb-short",
            )

        # None means the action cap dropped it; then no order exists.
        if order_id is not None:
            self._attempts += 1
            self._entries += 1
            self._since_entry = 0

    # --------------------------------------------------------------- callbacks

    def on_fill(self, ctx) -> None:
        if ctx.fill.position != 0:
            self._hold_bars = 0
            self._since_entry = 0

    def on_lifecycle(self, ctx) -> None:
        pass

    # ----------------------------------------------------------------- helpers

    def _read_book(self, ctx):
        """(best ask, best bid, top-5 ask size, top-5 bid size) or None.

        A missing row, a stale row, an empty side and a zero price are all
        ordinary states, not errors.
        """
        try:
            book = ctx.book()
        except Exception:
            return None
        if book is None:
            return None
        try:
            ask_prices = book.ask_prices
            bid_prices = book.bid_prices
            if len(ask_prices) == 0 or len(bid_prices) == 0:
                return None
            ask = float(ask_prices[0])
            bid = float(bid_prices[0])
            if ask <= 0.0 or bid <= 0.0 or ask <= bid:
                return None
            ask_depth = float(sum(book.ask_sizes[:5]))
            bid_depth = float(sum(book.bid_sizes[:5]))
            if ask_depth <= 0.0 or bid_depth <= 0.0:
                return None
            return ask, bid, ask_depth, bid_depth
        except Exception:
            return None

    def _signal(self):
        """(direction, |prior move|, failed extreme) or None.

        Direction is -1 short after absorbed buying, +1 long after absorbed
        selling. Every input is state already on `self`; the current bar is in.
        """
        mb = self.move_bars
        sb = self.stall_bars
        if len(self._closes) < mb + sb + 1:
            return None

        closes = list(self._closes)
        highs = list(self._highs)
        lows = list(self._lows)
        deltas = list(self._deltas)
        volumes = list(self._volumes)

        c_now = closes[-1]
        c_stall = closes[-1 - sb]
        c_prior = closes[-1 - sb - mb]
        if c_now <= 0.0 or c_stall <= 0.0 or c_prior <= 0.0:
            return None

        prior = (c_stall - c_prior) / c_prior
        move_abs = abs(prior)
        if move_abs < self.min_move:
            return None

        # The move must have stopped: no more than 30% added in the flow's
        # direction, no more than 50% given back (already collapsing is late).
        stall_ret = (c_now - c_stall) / c_stall
        if prior > 0.0:
            if stall_ret > self.stall_cap * prior:
                return None
            if stall_ret < -self.giveback_cap * prior:
                return None
        else:
            if stall_ret < self.stall_cap * prior:
                return None
            if stall_ret > -self.giveback_cap * prior:
                return None

        # "The best ask did not move up": no new extreme during the stall
        # window, with a 5bp tolerance for a wick. A stall measured on closes
        # alone is satisfied by a bar that traded through the offer and came
        # back, which is the opposite of absorption.
        if prior > 0.0:
            prior_extreme = max(highs[-(mb + sb):-sb])
            stall_extreme = max(highs[-sb:])
            if stall_extreme > prior_extreme * (1.0 + self.new_extreme_tol):
                return None
        else:
            prior_extreme = min(lows[-(mb + sb):-sb])
            stall_extreme = min(lows[-sb:])
            if stall_extreme < prior_extreme * (1.0 - self.new_extreme_tol):
                return None

        # Aggressive flow still leaning the way the move went: buyers still
        # lifting into a tape that will not advance (or sellers still hitting
        # one that will not fall). Normalised by traded volume, not fitted.
        traded = float(sum(volumes[-sb:]))
        if traded <= 0.0:
            return None
        flow = float(sum(deltas[-sb:])) / traded
        if prior > 0.0:
            if flow < self.min_flow:
                return None
        else:
            if flow > -self.min_flow:
                return None

        # Displayed size on the side the flow is leaning on. Relative to its own
        # recent distribution and to what it was when the flow started.
        own = self._ask_depth if prior > 0.0 else self._bid_depth
        if len(own) < max(self.depth_min, sb + 1):
            return None
        history = [v for v in list(own)[-self.depth_window:] if v > 0.0]
        if len(history) < self.depth_min:
            return None
        reference = median(history)
        now_size = own[-1]
        then_size = own[-1 - sb]
        if reference <= 0.0 or now_size <= 0.0 or then_size <= 0.0:
            return None
        if now_size < self.depth_ratio * reference:
            return None
        if now_size < self.refill_ratio * then_size:
            return None

        if prior > 0.0:
            return -1, move_abs, stall_extreme
        return 1, move_abs, stall_extreme

    def _flatten(self, ctx) -> None:
        """Cancel anything working and go flat, before the UTC day turns."""
        self._hold_bars = 0
        if not ctx.actionable:
            return
        if ctx.working_order_ids:
            ctx.cancel_all()
        if ctx.position != 0:
            ctx.close()
