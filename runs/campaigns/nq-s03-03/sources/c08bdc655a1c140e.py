"""Investigate whether an outsized intra-session move tends to continue or give back.

This strategy tests a **give-back** hypothesis with a focus on fading outsized
moves that occur after a sustained directional run. The idea is that an outsized
bar late in a trend often marks exhaustion, and price tends to revert.

Design:
- Rolling ATR (default 20 bars) adapts to current volatility.
- Trend filter: compute the net change over the last `trend_window` bars.
  Only consider outsized bars that occur **against** the prevailing trend,
  i.e., an outsized down bar after an up trend, or an outsized up bar after a
  down trend. This targets exhaustion moves rather than continuation.
- Outsized bar: range >= range_mult * ATR (default 2.0).
- Entry: on the next bar, enter in the opposite direction of the outsized bar
  (fade it). Use a market order for immediate execution.
- Bracket exits use ATR-scaled offsets, with target slightly larger than stop
  to favor the reversion.
- Time filter: only trade during the first 80% of the session to avoid
  late-session noise and ensure enough time for the reversion to play out.
- One position at a time, no overlapping signals.
"""

from __future__ import annotations


class Strategy:
    """Fade outsized exhaustion moves against the prevailing trend."""

    def __init__(self, *, interval: str = "5m", atr_window: int = 20,
                 trend_window: int = 10, range_mult: float = 2.0,
                 session_frac: float = 0.8, stop_atr: float = 1.0,
                 target_atr: float = 1.5, size: int = 1):
        self.interval = interval
        self.atr_window = atr_window
        self.trend_window = trend_window
        self.range_mult = range_mult
        self.session_frac = session_frac
        self.stop_atr = stop_atr
        self.target_atr = target_atr
        self.size = size

        # Per-session state.
        self._session = None
        self._trs: list[float] = []
        self._prev_close = None
        self._pending_fade = False
        self._fade_direction = 0  # +1 for fade up (short after outsized up), -1 for fade down

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Reset state when the session changes.
        if ctx.session != self._session:
            self._session = ctx.session
            self._trs = []
            self._prev_close = None
            self._pending_fade = False
            self._fade_direction = 0

        history = ctx.history(self.interval)
        if len(history) < 3:
            return

        bar = history[-1]

        # Update true range series.
        if self._prev_close is None:
            self._prev_close = bar.open
        tr = max(bar.high - bar.low,
                 abs(bar.high - self._prev_close),
                 abs(bar.low - self._prev_close))
        self._trs.append(tr)
        self._prev_close = bar.close

        if len(self._trs) < 3:
            return

        window = self._trs[-self.atr_window:]
        atr = sum(window) / len(window)
        if atr <= 0:
            return

        # One position at a time.
        if ctx.position or ctx.working_order_ids:
            return

        # Time filter: only trade during the first session_frac of the session.
        session_elapsed = (ctx.ts - ctx.session_start) / (ctx.session_end - ctx.session_start)
        if session_elapsed > self.session_frac:
            return

        # If we have a pending fade signal from the previous bar, execute now.
        if self._pending_fade:
            if self._fade_direction > 0:
                ctx.buy(
                    size=self.size,
                    stop_offset=self.stop_atr * atr,
                    target_offset=self.target_atr * atr,
                    tag="fade-exhaustion-long",
                )
            else:
                ctx.sell(
                    size=self.size,
                    stop_offset=self.stop_atr * atr,
                    target_offset=self.target_atr * atr,
                    tag="fade-exhaustion-short",
                )
            self._pending_fade = False
            self._fade_direction = 0
            return

        # Identify outsized bars against the trend.
        if len(history) < self.trend_window + 2:
            return

        # Compute trend over the last trend_window bars (excluding the current bar).
        trend_bars = history[-self.trend_window - 1:-1]
        if len(trend_bars) < self.trend_window:
            return

        trend_change = trend_bars[-1].close - trend_bars[0].close

        bar_range = bar.high - bar.low
        if bar_range < self.range_mult * atr:
            return

        # Outsized bar detected. Determine direction.
        if bar.close > bar.open:
            # Outsized up bar. Fade it if trend is down (exhaustion up move).
            if trend_change < 0:
                self._pending_fade = True
                self._fade_direction = -1  # sell (fade up)
        else:
            # Outsized down bar. Fade it if trend is up (exhaustion down move).
            if trend_change > 0:
                self._pending_fade = True
                self._fade_direction = +1  # buy (fade down)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
