"""A refined momentum strategy based on the best prior run (#0).

The previous best used 1m bars, an 8-bar ATR lookback, entry when the last
bar's body was at least 0.25 * ATR with delta confirming, and ATR-scaled
bracket offsets (stop 1.5 ATR / target 2.5 ATR). This version keeps that
proven structure but makes three deliberate changes:

1. The entry threshold is relaxed slightly from 0.25 ATR to 0.20 ATR to
   capture a few more qualifying momentum bars.
2. The target is widened from 2.5 ATR to 3.0 ATR, giving winning trades more
   room while the stop remains 1.5 ATR.
3. New entries are suppressed in the final 10% of a session. Session length
   is known in advance via ctx.session_end, so this is not lookahead; it
   avoids questionable late-session signals and the risk of carrying a fresh
   position into the boundary.

The bracket is still armed by the engine at the instant the entry fills, so
the position is protected without waiting for the next callback.
"""

from __future__ import annotations


class Strategy:
    """Momentum with ATR-scaled filter, wider target, and late-session gate."""

    def __init__(
        self,
        *,
        interval: str = "1m",
        lookback: int = 8,
        entry_frac: float = 0.20,
        stop_mult: float = 1.5,
        target_mult: float = 3.0,
        size: int = 1,
        session_margin_frac: float = 0.10,
    ):
        self.interval = interval
        self.lookback = lookback
        self.entry_frac = entry_frac
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        self.session_margin_frac = session_margin_frac

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        # Do not open a fresh position in the last part of a session.
        session_len = ctx.session_end - ctx.session_start
        if session_len > 0:
            remaining = ctx.session_end - ctx.ts
            if remaining < session_len * self.session_margin_frac:
                return

        recent = ctx.history(self.interval).last(self.lookback + 1)
        if len(recent) < self.lookback + 1:
            return

        prior = recent[:-1]
        trs = []
        prev_close = prior[0].close
        for bar in prior[1:]:
            trs.append(
                max(
                    bar.high - bar.low,
                    abs(bar.high - prev_close),
                    abs(bar.low - prev_close),
                )
            )
            prev_close = bar.close

        if not trs:
            return

        atr = sum(trs) / len(trs)
        if atr <= 0:
            return

        last = recent[-1]
        body = last.close - last.open
        threshold = self.entry_frac * atr

        stop = self.stop_mult * atr
        target = self.target_mult * atr

        # Price direction and signed order flow must agree.
        if body >= threshold and last.delta >= 0:
            ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="momentum-long",
            )
        elif body <= -threshold and last.delta <= 0:
            ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="momentum-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
