"""Absorption fade on the OKX BTC-USDT perpetual.

The claim under test: after a directional move, aggressive flow that runs into
displayed liquidity which *holds* tends to mark a turn rather than a
continuation. This file implements the narrow, symmetric version of that claim
and nothing else -- no regime model, no market making, one instrument.

What the signal is, mechanically, evaluated at the close of every 2m bar. All of
it is computed from state kept on `self`, because `ctx.history` is a bounded
window and the book has no history at all -- `ctx.book()` returns one row and
nothing else.

  1. Prior move. close now vs. close `flow_window` bars ago, as a fraction.
     >= +12bp for a short setup, <= -12bp for a long one.

  2. Unusually large flow. sum(buy_volume - sell_volume) / sum(volume) over the
     same window -- i.e. `bar.delta` normalised by total traded volume. That is
     a free normalisation by activity rather than a fitted one, and it needs no
     tick subscription: `bar.delta` is accumulated per tick by the bar builder.
     |flow| > 0.22 means roughly 61% of the window's volume was one-sided.

  3. The touch does not give way. Price rose over the window, but the best ask
     now is not above the best ask `stall_bars` ago by more than 4bp: the lift
     stopped without the offer lifting. Symmetric for the bid after a fall.

  4. Size holds or refills. Top-3 ask size now is at least 70% of the median
     top-3 ask size over the window. A level being eaten shows up here as a
     collapse; a level that refills does not. This is the "displayed liquidity
     holds" half of the claim, and it is the half that `ctx.book()` exists for.

Entry is the fade: short absorbed buying, long absorbed selling, one position at
a time, market order with a bracket so protection exists from the instant of the
fill rather than from the next callback. Stops 35bp, targets 50bp, both given as
*offsets* and never as absolute levels -- a market order slips, and an absolute
stop derived from the price I could see lands on the wrong side of the fill by a
tick. With this fee schedule the break-even hit rate is about 44%.

Everything is closed inside its own UTC day: no new entry with less than 30
minutes left, and five minutes before `ctx.session_end` any bracket is cancelled
and the position is flattened. Funding at 00:00 is therefore never paid by a
carried position. No reversal is ever taken directly; a new signal is only acted
on from flat, so a change of view is two separate trades at worst.

Choices the brief deliberately left open, and why:

  * Market entries, not resting limits. Absorption is exactly the case where a
    posted order sits *behind* a large displayed level that is holding, so a
    maker entry is least likely to fill precisely when the signal is strongest.
    Taker entry costs ~2bp against a 50bp target. The target leg is the engine's
    own bracket (a limit, so maker, 0bp); only the stop or the day-end flatten
    pays taker.
  * No tick subscription. `bar.delta` carries the same signed flow at bar
    resolution, and a tick subscription disables the engine's event skip for
    roughly 5x the wall-clock cost, for a sub-bar sequence this signal does not
    use.
  * At most 5 entries a session, at least 10 minutes apart. That keeps the
    per-session action count far below the cap of 64 and stops one noisy hour
    from being traded five times.
  * `emit_empty=True` on the bar subscription. The flatten-before-the-day-ends
    rule needs a callback to fire on, and a quiet stretch near 24:00 would
    otherwise leave the position on overnight.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    """Fade aggressive flow that fails against displayed book liquidity."""

    def __init__(
        self,
        interval: str = "2m",
        flow_window: int = 8,
        stall_bars: int = 3,
        flow_thresh: float = 0.22,
        move_thresh: float = 0.0012,
        stall_tol: float = 0.0004,
        size_hold: float = 0.70,
        size: int = 100,
        stop_pct: float = 0.0035,
        target_pct: float = 0.0050,
        max_entries: int = 5,
        min_spacing_bars: int = 5,
        entry_cutoff_ns: int = 1_800_000_000_000,
        flat_margin_ns: int = 300_000_000_000,
    ) -> None:
        self.interval = interval
        self.flow_window = max(3, int(flow_window))
        self.stall_bars = max(1, min(int(stall_bars), self.flow_window - 1))
        self.flow_thresh = float(flow_thresh)
        self.move_thresh = float(move_thresh)
        self.stall_tol = float(stall_tol)
        self.size_hold = float(size_hold)
        self.size = max(1, int(size))
        self.stop_pct = float(stop_pct)
        self.target_pct = float(target_pct)
        self.max_entries = max(1, int(max_entries))
        self.min_spacing_bars = max(0, int(min_spacing_bars))
        self.entry_cutoff_ns = int(entry_cutoff_ns)
        self.flat_margin_ns = int(flat_margin_ns)

        self._session = None
        self._entries_today = 0
        self._bars_since_entry = 10 ** 9

        # One entry per closed bar, trimmed to the window we actually read.
        self._closes: deque[float] = deque(maxlen=self.flow_window + 1)
        self._vols: deque[int] = deque(maxlen=self.flow_window)
        self._deltas: deque[int] = deque(maxlen=self.flow_window)
        self._asks: deque[tuple[float, float] | None] = deque(
            maxlen=self.flow_window
        )
        self._bids: deque[tuple[float, float] | None] = deque(
            maxlen=self.flow_window
        )

    # ------------------------------------------------------------------ wiring

    def setup(self, ctx) -> None:
        # emit_empty so the day-end flatten always has a callback to fire on.
        ctx.subscribe(self.interval, emit_empty=True)
        # No extra callback; it only makes ctx.book() readable.
        ctx.subscribe("book")

    # ------------------------------------------------------------------- bars

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        if ctx.session != self._session:
            self._session = ctx.session
            self._entries_today = 0
            self._bars_since_entry = 10 ** 9

        remaining = ctx.session_end - ctx.ts
        if remaining <= self.flat_margin_ns:
            # Out of time for this UTC day: nothing may be carried across it.
            self._flatten(ctx)
            return

        ask, bid = self._read_book(ctx)

        # Accumulate on every bar, warmup included -- there is nothing fitted
        # here, the warmup bars just prime the rolling windows.
        self._closes.append(float(bar.close))
        self._vols.append(int(bar.volume))
        self._deltas.append(int(bar.delta))
        self._asks.append(ask)
        self._bids.append(bid)
        if self._bars_since_entry < 10 ** 9:
            self._bars_since_entry += 1

        if not ctx.actionable:
            return
        if ctx.position or ctx.working_order_ids:
            return
        if self._entries_today >= self.max_entries:
            return
        if self._bars_since_entry < self.min_spacing_bars:
            return
        if remaining < self.entry_cutoff_ns:
            return
        if ask is None or bid is None:
            return

        direction = self._signal()
        if direction == 0:
            return

        price = float(ctx.last_price)
        if price <= 0.0:
            return
        stop_off = price * self.stop_pct
        target_off = price * self.target_pct
        if stop_off <= 0.0 or target_off <= 0.0:
            return

        if direction > 0:
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop_off,
                target_offset=target_off,
                tag="absorption-long",
            )
        else:
            order_id = ctx.sell(
                size=self.size,
                stop_offset=stop_off,
                target_offset=target_off,
                tag="absorption-short",
            )

        # None means the action cap dropped it; then no order exists.
        if order_id is not None:
            self._entries_today += 1
            self._bars_since_entry = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    # ---------------------------------------------------------------- helpers

    def _read_book(self, ctx):
        """Best ask/bid price and top-3 size, or (None, None).

        The row may be absent, may be stale (over a second old reads as None),
        and may have an empty side. All three are ordinary states, not errors.
        """
        try:
            book = ctx.book()
            if book is None:
                return None, None
            ask_prices = book.ask_prices
            ask_sizes = book.ask_sizes
            bid_prices = book.bid_prices
            bid_sizes = book.bid_sizes
            if len(ask_prices) == 0 or len(bid_prices) == 0:
                return None, None
            ask = (float(ask_prices[0]), float(sum(ask_sizes[:3])))
            bid = (float(bid_prices[0]), float(sum(bid_sizes[:3])))
            if ask[0] <= 0.0 or bid[0] <= 0.0:
                return None, None
            return ask, bid
        except Exception:
            return None, None

    def _signal(self) -> int:
        """+1 long after absorbed selling, -1 short after absorbed buying, 0 no.

        Reads only what is on `self`; the current bar's book row is already in
        the deques by the time this is called.
        """
        if len(self._closes) < self.flow_window + 1:
            return 0
        closes = list(self._closes)
        ref = closes[0]
        now_close = closes[-1]
        if ref <= 0.0 or now_close <= 0.0:
            return 0

        move = (now_close - ref) / ref
        if abs(move) < self.move_thresh:
            return 0

        volume = 0
        delta = 0
        for v in self._vols:
            volume += v
        for d in self._deltas:
            delta += d
        if volume <= 0:
            return 0
        flow = float(delta) / float(volume)
        if abs(flow) < self.flow_thresh:
            return 0
        if move > 0.0 and flow <= 0.0:
            return 0
        if move < 0.0 and flow >= 0.0:
            return 0

        k = self.stall_bars
        if len(self._asks) < k + 1 or len(self._bids) < k + 1:
            return 0

        if move > 0.0:
            # Absorbed buying: the offer did not lift while price rose, and the
            # displayed size on that side held against the flow.
            touch_now = self._asks[-1]
            touch_then = self._asks[-1 - k]
            if touch_now is None or touch_then is None:
                return 0
            if touch_now[0] > touch_then[0] + self.stall_tol * now_close:
                return 0
            sizes = [a[1] for a in self._asks if a is not None]
            if len(sizes) < 3:
                return 0
            median = sorted(sizes)[len(sizes) // 2]
            if median <= 0.0 or touch_now[1] < self.size_hold * median:
                return 0
            return -1

        # Absorbed selling: mirror image on the bid.
        touch_now = self._bids[-1]
        touch_then = self._bids[-1 - k]
        if touch_now is None or touch_then is None:
            return 0
        if touch_now[0] < touch_then[0] - self.stall_tol * now_close:
            return 0
        sizes = [b[1] for b in self._bids if b is not None]
        if len(sizes) < 3:
            return 0
        median = sorted(sizes)[len(sizes) // 2]
        if median <= 0.0 or touch_now[1] < self.size_hold * median:
            return 0
        return 1

    def _flatten(self, ctx) -> None:
        """Cancel anything still working and go flat, before the day turns."""
        if not ctx.actionable:
            return
        if ctx.working_order_ids:
            ctx.cancel_all()
        if ctx.position:
            ctx.close()
