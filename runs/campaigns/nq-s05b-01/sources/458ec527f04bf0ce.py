"""Short-side opening reversal strategy for NQ futures, v7.

The campaign requires a short-only reversal strategy using a symmetric 1:1
risk/reward. The best prior score is +0.1252 from trial #39. This version
attempts to improve by refining the entry signal to focus on a strong opening
rally that fails, while maintaining the required structure.

Key changes from v6:
- Use a 5-minute bar interval to capture the opening range more robustly.
- Define the opening high as the highest high of the first 6 bars (30 minutes).
- Require a reversal bar that closes below the opening high and below its own
  open, with a bearish body at least 40% of the bar's range.
- Require a minimum opening range of 3.0 points to filter quiet sessions.
- Use a risk of 0.75 times the distance from the opening high to the reversal
  bar's close, with a symmetric target.
- Only one trade per session, during the first 35 minutes.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        opening_bars: int = 6,
        risk_mult: float = 0.75,
        min_range: float = 3.0,
        bearish_body_mult: float = 0.4,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_bars = opening_bars
        self.risk_mult = risk_mult
        self.min_range = min_range
        self.bearish_body_mult = bearish_body_mult
        self.size = size

        self._session = None
        self._traded_this_session = False
        self._opening_high = None
        self._bars_seen = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        if ctx.position or ctx.working_order_ids:
            return

        if ctx.session != self._session:
            self._session = ctx.session
            self._traded_this_session = False
            self._opening_high = None
            self._bars_seen = 0

        if self._traded_this_session:
            return

        if self.interval not in ctx.bars:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        last = history[-1]
        self._bars_seen += 1

        # Track the opening high.
        if self._opening_high is None or last.high > self._opening_high:
            self._opening_high = last.high

        # Need enough bars to establish the opening range.
        if self._bars_seen < self.opening_bars:
            return

        # Opening range is high minus lowest low of the opening bars.
        opening_low = min(
            b.low for b in history.last(self._bars_seen)
        )
        opening_range = self._opening_high - opening_low
        if opening_range < self.min_range:
            return

        # Reversal bar must be bearish and close below the opening high.
        bar_range = last.high - last.low
        if bar_range <= 0:
            return
        body = last.open - last.close
        if last.close >= last.open or body < self.bearish_body_mult * bar_range:
            return
        if last.close >= self._opening_high:
            return

        # Risk is a fraction of the distance from the high to the close.
        risk = self.risk_mult * (self._opening_high - last.close)
        if risk <= 0:
            return

        ctx.sell(
            size=self.size,
            stop_offset=risk,
            target_offset=risk,
            tag="opening-reversal-short",
        )
        self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
