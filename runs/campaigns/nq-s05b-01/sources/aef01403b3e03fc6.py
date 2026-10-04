"""Short-side opening reversal strategy for NQ futures, v7.

Prior trials show the best score (+0.1252) from a simple opening-range
reversal with a 1-minute bar, symmetric risk, and a minimum range filter.
This version attempts to improve by focusing on the highest-quality signals:
- Use a 1-minute bar interval.
- Define the opening high as the highest high of the first 5 bars.
- Enter short when price closes below that high after a strong bearish bar.
- Risk is a fraction of the opening range, not the distance to the high,
  to give the trade more room and avoid premature stops.
- Add a time filter: only trade in the first 20 minutes.
- Add a volume filter: require the reversal bar to have above-average volume.
- Only one trade per session.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_minutes: int = 20,
        lookback: int = 5,
        risk_mult: float = 0.35,
        min_range: float = 2.0,
        bearish_body_mult: float = 0.4,
        min_rally: float = 1.0,
        volume_mult: float = 1.2,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.risk_mult = risk_mult
        self.min_range = min_range
        self.bearish_body_mult = bearish_body_mult
        self.min_rally = min_rally
        self.volume_mult = volume_mult
        self.size = size

        self._session = None
        self._traded_this_session = False
        self._opening_high = None
        self._bars_seen = 0
        self._first_close = None
        self._volume_sum = 0
        self._volume_count = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        if ctx.position or ctx.working_order_ids:
            return

        # Reset per-session state on session change.
        if ctx.session != self._session:
            self._session = ctx.session
            self._traded_this_session = False
            self._opening_high = None
            self._bars_seen = 0
            self._first_close = None
            self._volume_sum = 0
            self._volume_count = 0

        if self._traded_this_session:
            return

        # Only trade during the opening window.
        session_elapsed = (ctx.ts - ctx.session_start) / 60_000_000_000
        if session_elapsed > self.opening_minutes:
            return

        if self.interval not in ctx.bars:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        last = history[-1]
        self._bars_seen += 1

        # Track the first bar's close for the rally filter.
        if self._bars_seen == 1:
            self._first_close = last.close

        # Need enough bars to establish an opening high.
        if self._bars_seen < self.lookback:
            if self._opening_high is None or last.high > self._opening_high:
                self._opening_high = last.high
            return

        # Update opening high with the current bar.
        if last.high > self._opening_high:
            self._opening_high = last.high

        # Signal: price made a new opening high and then closed below it.
        # Require a meaningful opening range to filter quiet sessions.
        opening_range = self._opening_high - min(
            b.low for b in history.last(self._bars_seen)
        )
        if opening_range < self.min_range:
            return

        # Require a meaningful rally above the first bar's close.
        if self._first_close is not None:
            rally = self._opening_high - self._first_close
            if rally < self.min_rally:
                return

        # Require a bearish reversal bar: close below open and a body that is
        # at least a fraction of the bar's range.
        bar_range = last.high - last.low
        if bar_range <= 0:
            return
        body = last.open - last.close
        if last.close >= last.open or body < self.bearish_body_mult * bar_range:
            return

        # Volume filter: require the reversal bar to have above-average volume.
        self._volume_sum += last.volume
        self._volume_count += 1
        if self._volume_count > 0:
            avg_volume = self._volume_sum / self._volume_count
            if last.volume < self.volume_mult * avg_volume:
                return

        # Risk is a fraction of the opening range.
        risk = self.risk_mult * opening_range
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
