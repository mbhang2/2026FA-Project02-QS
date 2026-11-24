"""Absorption of aggressive flow by displayed book liquidity, BTC perpetual.

The claim under test: after a directional move, aggressive flow that fails to
push price further -- while the opposing side of the visible book stays deep --
marks a reversal more often than a continuation.

How this file operationalises each piece, using only data already seen:

- **Prior move.** Close-to-close over `prior_bars` (15) one-minute bars, ending
  `recent_bars` (3) bars ago, so the recent stall is measured separately from
  the impulse. The threshold is absolute (20 bp) AND volatility-scaled (>= 1.0 x
  the recent mean absolute one-minute move carried out over the window), so a
  quiet tape and a wild one are not asked the same question.

- **"Unusually large" flow.** `bar.delta` -- aggressive buy minus aggressive
  sell volume, accumulated by the bar builder -- divided by the bar's total
  volume, summed over the last 3 bars. No tick subscription: `bar.delta` carries
  exactly this and a tick feed costs roughly 5x the wall clock for the same
  number. `unknown_volume` is always zero on this feed, so delta is complete.

- **"Holds" / "refills".** At every bar close the top five levels on each side
  of `ctx.book()` are summed into a rolling 30-sample history. Absorption
  requires the opposing side's displayed depth to be at least 1.40x its own
  median over that history. Because the window already contains the bars in
  which the aggressive flow was hitting, a depth that is above its own norm now
  is depth that survived the flow and refilled, not depth that was never tested.

- **"Best price does not move."** The stall test is the price-space form of it:
  over the last 3 bars the price must have advanced at most 25% of the prior
  move (and given back no more than 50%), while flow stayed one-sided. Buying
  that does not lift the offer is buying the offer absorbed.

- **Direction.** Short after absorbed buying on a rise, long after absorbed
  selling on a fall. Never with the flow: that would be the opposite claim.

Execution and costs: market entry (taker, 2 bp), bracket armed by the engine at
the fill with a 25 bp target resting as a limit (maker, 0 bp) and a 30 bp
protective stop (taker on the way out). A 20-minute time stop and a 5-minute
pre-close flatten cap the tail. Entries are refused within 35 minutes of the
00/08/16 UTC funding times so no trade pays funding, and at most 6 entries a
session keep the 64-action cap comfortable. One position at a time; a new
signal while a position is open is ignored, never reversed into, so every trade
opens from flat and closes to flat inside its own UTC day.
"""

from __future__ import annotations

import math
from statistics import median

NS_PER_SECOND = 1_000_000_000
NS_PER_MINUTE = 60 * NS_PER_SECOND
_FUNDING_SECONDS = (28_800, 57_600, 86_400)


class Strategy:
    """Fade aggressive flow that the visible book absorbed."""

    def __init__(
        self,
        *,
        interval: str = "1m",
        size: int = 100,
        prior_bars: int = 15,
        recent_bars: int = 3,
        min_prior_move: float = 0.0020,
        vol_mult: float = 1.0,
        min_flow: float = 0.20,
        stall_frac: float = 0.25,
        giveback_frac: float = 0.50,
        depth_mult: float = 1.40,
        depth_window: int = 30,
        depth_min_samples: int = 12,
        stop_frac: float = 0.0030,
        target_frac: float = 0.0025,
        max_hold_minutes: int = 20,
        flat_margin_s: int = 300,
        funding_margin_s: int = 2100,
        min_time_to_close_s: int = 2100,
        max_trades_per_session: int = 6,
    ) -> None:
        self.interval = interval
        self.size = size
        self.prior_bars = prior_bars
        self.recent_bars = recent_bars
        self.min_prior_move = min_prior_move
        self.vol_mult = vol_mult
        self.min_flow = min_flow
        self.stall_frac = stall_frac
        self.giveback_frac = giveback_frac
        self.depth_mult = depth_mult
        self.depth_window = depth_window
        self.depth_min_samples = depth_min_samples
        self.stop_frac = stop_frac
        self.target_frac = target_frac
        self.max_hold_minutes = max_hold_minutes
        self.flat_margin_s = flat_margin_s
        self.funding_margin_s = funding_margin_s
        self.min_time_to_close_s = min_time_to_close_s
        self.max_trades_per_session = max_trades_per_session

        # Rolling state, kept here because ctx.history() is a bounded window and
        # because the book's own history is not available from ctx at all.
        self.closes: list[float] = []
        self.deltas: list[float] = []
        self.volumes: list[float] = []
        self.ask_depth: list[float] = []
        self.bid_depth: list[float] = []
        self._cap = 128

        self.entry_ts: int | None = None
        self.session: int | None = None
        self.trades_today = 0

    # ---------------------------------------------------------------- wiring

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval, emit_empty=True)
        # No callback of its own; read at each bar close with ctx.book().
        ctx.subscribe("book")

    # -------------------------------------------------------------- rolling

    def _record_bar(self, bar) -> None:
        self.closes.append(float(bar.close))
        self.deltas.append(float(bar.delta))
        self.volumes.append(float(bar.volume))
        if len(self.closes) > self._cap:
            self.closes.pop(0)
            self.deltas.pop(0)
            self.volumes.pop(0)

    def _record_book(self, book) -> None:
        ask = float(sum(book.ask_sizes[:5])) if book.ask_sizes else 0.0
        bid = float(sum(book.bid_sizes[:5])) if book.bid_sizes else 0.0
        self.ask_depth.append(ask)
        self.bid_depth.append(bid)
        if len(self.ask_depth) > self.depth_window:
            self.ask_depth.pop(0)
            self.bid_depth.pop(0)

    def _vol_scale(self, k: int) -> float:
        """Typical k-bar move implied by the last 30 one-bar returns."""
        closes = self.closes
        n = len(closes)
        m = min(30, n - 1)
        if m < 5:
            return 0.0
        total = 0.0
        for i in range(n - m, n):
            prev = closes[i - 1]
            if prev > 0:
                total += abs(closes[i] - prev) / prev
        mean_abs = total / m
        return 1.25 * mean_abs * math.sqrt(k)

    # ---------------------------------------------------------------- run

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]
        self._record_bar(bar)

        book = ctx.book()
        if book is not None:
            self._record_book(book)

        if ctx.session != self.session:
            self.session = ctx.session
            self.trades_today = 0

        if ctx.position != 0:
            if self.entry_ts is None:
                self.entry_ts = ctx.ts
            held = ctx.ts - self.entry_ts
            time_left = ctx.session_end - ctx.ts
            if (
                held >= self.max_hold_minutes * NS_PER_MINUTE
                or time_left <= self.flat_margin_s * NS_PER_SECOND
            ):
                self._flatten(ctx)
            return

        self.entry_ts = None

        if not ctx.actionable:
            return
        if ctx.working_order_ids:
            return
        if self.trades_today >= self.max_trades_per_session:
            return
        if ctx.session_end - ctx.ts < self.min_time_to_close_s * NS_PER_SECOND:
            return
        if self._near_funding(ctx.ts):
            return
        self._maybe_enter(ctx)

    # -------------------------------------------------------------- entry

    def _maybe_enter(self, ctx) -> None:
        closes = self.closes
        r = self.recent_bars
        k = self.prior_bars
        n = len(closes)
        if n < r + k + 2:
            return

        c_now = closes[-1]
        c_mid = closes[-1 - r]
        c_old = closes[-1 - r - k]
        if c_mid <= 0 or c_old <= 0:
            return

        ret_prior = (c_mid - c_old) / c_old
        if abs(ret_prior) < self.min_prior_move:
            return
        scale = self._vol_scale(k)
        if scale > 0.0 and abs(ret_prior) < self.vol_mult * scale:
            return

        ret_recent = (c_now - c_mid) / c_mid

        # Aggressive flow over the stall window, normalised by traded volume.
        total_vol = float(sum(self.volumes[-r:]))
        if total_vol <= 0.0:
            return
        flow = float(sum(self.deltas[-r:])) / total_vol

        rising = ret_prior > 0.0

        if rising:
            if flow < self.min_flow:
                return
            # Price must have stopped making progress despite the buying...
            if ret_recent > self.stall_frac * ret_prior:
                return
            # ...but it must not already be collapsing back (entry too late).
            if ret_recent < -self.giveback_frac * ret_prior:
                return
            depths = self.ask_depth
        else:
            if flow > -self.min_flow:
                return
            if ret_recent < self.stall_frac * ret_prior:
                return
            if ret_recent > -self.giveback_frac * ret_prior:
                return
            depths = self.bid_depth

        # Displayed liquidity on the side the flow was leaning on: deep now,
        # relative to its own recent norm, after having been leaned on.
        if len(depths) < self.depth_min_samples:
            return
        ref = median(depths[-self.depth_window:])
        if ref <= 0.0:
            return
        if depths[-1] < self.depth_mult * ref:
            return

        price = ctx.last_price
        if price <= 0.0:
            return
        stop_offset = price * self.stop_frac
        target_offset = price * self.target_frac

        if rising:
            order_id = ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="absorb-short",
            )
        else:
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="absorb-long",
            )
        if order_id is not None:
            self.trades_today += 1
            self.entry_ts = ctx.ts

    # --------------------------------------------------------------- exits

    def _flatten(self, ctx) -> None:
        self.entry_ts = None
        if ctx.position == 0:
            return
        ctx.cancel_all()
        ctx.close()

    def _near_funding(self, ts: int) -> bool:
        sod = (ts // NS_PER_SECOND) % 86_400
        for boundary in _FUNDING_SECONDS:
            gap = boundary - sod
            if 0 <= gap < self.funding_margin_s:
                return True
        return False

    # ----------------------------------------------------------- callbacks

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
