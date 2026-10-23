"""Next strategy: volume-confirmed quiet three-bar breakout.

Keeps the #7 core (three-bar breakout with cumulative delta confirmation,
20-bar trend anchor, and ATR-scaled bracket) and adds two quality filters that
#7 lacked:

1. Preparation contraction -- the three bars immediately before the signal must
   have a high-low range smaller than the trailing ATR. This enters breakouts
   from a tight base rather than after an already expanded range, which removes
   a batch of marginal breakouts where the breakout level is almost the same
   thing as the recent high.

2. Volume surge -- the signal bar must trade at least `volume_mult` times the
   average volume over the trailing 20-bar window. The bar feed exposes volume
   without a tick subscription, so this filters out price moves on thin trade.

Prior trials:
#0 simple close-to-close momentum: 1,241 trades, +3.3002
#1 ATR-scaled + short trend filter: 895 trades, +4.0299
#2 adding 20-bar range + volume filter: 527 trades, +3.0401
#3 one-bar breakout: 846 trades, +4.6289
#4 two-bar breakout: 830 trades, +4.8108
#5 three-bar breakout: 813 trades, +4.9760
#6 five-bar breakout: 770 trades, +4.7048
#7 three-bar breakout + cumulative breakout delta: 741 trades, +5.0112
#8 clearance buffer filtered nothing (identical trades to #7)

The next filter therefore changes the character of the pre-breakout window and
the participation of the signal bar itself.
"""

from __future__ import annotations


class Strategy:
    """Delta-confirmed, volume-confirmed quiet three-bar breakout."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 20,
        breakout_bars: int = 3,
        stop_mult: float = 1.0,
        target_mult: float = 1.5,
        prep_mult: float = 1.0,
        volume_mult: float = 1.2,
        size: int = 1,
    ):
        self.interval = interval
        self.lookback = lookback
        self.breakout_bars = breakout_bars
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.prep_mult = prep_mult
        self.volume_mult = volume_mult
        self.size = size

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < self.lookback + 1:
            return

        window = history.last(self.lookback + 1)
        if len(window) < self.lookback + 1:
            return
        if len(window) < self.breakout_bars + 1:
            return

        last = window[-1]
        prior = window[:-1]

        # Trend anchor: average close over the trailing window before the
        # signal bar.
        trend = sum(b.close for b in prior) / self.lookback

        # ATR over the full window, used for the bracket and the contraction
        # filter.
        trs = []
        for i in range(1, len(window)):
            bar = window[i]
            prev_close = window[i - 1].close
            tr = max(
                bar.high - bar.low,
                abs(bar.high - prev_close),
                abs(bar.low - prev_close),
            )
            trs.append(tr)

        atr = sum(trs) / len(trs)
        if atr <= 0:
            return

        stop = atr * self.stop_mult
        target = atr * self.target_mult
        if stop <= 0 or target <= 0:
            return

        # The prior `breakout_bars` bars define the immediate range to clear.
        recent = window[-self.breakout_bars - 1:-1]
        recent_high = max(b.high for b in recent)
        recent_low = min(b.low for b in recent)
        prep_range = recent_high - recent_low

        # New filter 1: quiet preparation. The pre-breakout range must be
        # contained relative to recent volatility.
        if prep_range > self.prep_mult * atr:
            return

        # New filter 2: volume surge on the signal bar.
        avg_volume = sum(b.volume for b in prior) / self.lookback
        if avg_volume <= 0 or last.volume < self.volume_mult * avg_volume:
            return

        last_delta = last.delta
        recent_delta = sum(b.delta for b in recent)

        if (
            last.close > last.open
            and last_delta > 0
            and recent_delta > 0
            and last.close > trend
            and last.close > recent_high
        ):
            ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="quiet-breakout-long",
            )
        elif (
            last.close < last.open
            and last_delta < 0
            and recent_delta < 0
            and last.close < trend
            and last.close < recent_low
        ):
            ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="quiet-breakout-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
