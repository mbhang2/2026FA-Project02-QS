"""Short-side opening reversal strategy for NQ futures, v7.

This version places a sell limit order at the opening high after a brief
lookback period, with a symmetric stop and target based on the rally size.
The goal is to capture the reversal described in the campaign while
maintaining exposure in most sessions (to satisfy the 70% floor).

Key design:
- 1-minute bars, opening window 15 minutes.
- After 5 bars, compute the opening high and the first bar's close.
- If the rally (high - first close) is at least 0.5 points and the opening
  range is at least 1.0 point, place a sell limit order slightly below the
  high (to improve fill probability).
- Risk is a fraction of the rally, with a minimum of 1.5 points.
- The bracket uses absolute stop and target prices, symmetric around the
  limit price.
- The order is DAY and rests until filled or session close, ensuring
  exposure in nearly every session.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_minutes: int = 15,
        lookback: int = 5,
        min_rally: float = 0.5,
        min_range: float = 1.0,
        risk_mult: float = 0.5,
        min_risk: float = 1.5,
        limit_offset: float = 0.25,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.min_rally = min_rally
        self.min_range = min_range
        self.risk_mult = risk_mult
        self.min_risk = min_risk
        self.limit_offset = limit_offset
        self.size = size

        self._session = None
        self._order_placed = False
        self._first_close = None
        self._bars_seen = 0

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
            self._order_placed = False
            self._first_close = None
            self._bars_seen = 0

        if self._order_placed:
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

        # Track the first bar's close.
        if self._bars_seen == 1:
            self._first_close = last.close

        # Need enough bars to establish the opening high.
        if self._bars_seen < self.lookback:
            return

        # Compute opening high and range from the first `lookback` bars.
        recent = history.last(self.lookback)
        opening_high = max(b.high for b in recent)
        opening_low = min(b.low for b in recent)
        opening_range = opening_high - opening_low

        # Filters to avoid trading in very quiet or flat openings.
        if opening_range < self.min_range:
            return
        if self._first_close is None:
            return
        rally = opening_high - self._first_close
        if rally < self.min_rally:
            return

        # Risk is a fraction of the rally, with a floor.
        risk = max(self.min_risk, self.risk_mult * rally)

        # Place a sell limit order slightly below the opening high.
        limit_price = opening_high - self.limit_offset
        stop_price = limit_price + risk
        target_price = limit_price - risk

        ctx.sell(
            size=self.size,
            type="limit",
            limit=limit_price,
            stop=stop_price,
            target=target_price,
            tif="DAY",
            tag="opening-reversal-short",
        )
        self._order_placed = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
