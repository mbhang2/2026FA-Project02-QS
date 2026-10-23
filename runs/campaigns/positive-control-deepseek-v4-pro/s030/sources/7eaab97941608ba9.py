"""Builds on the best prior trial (#34): ATR-scaled momentum with trigger-bar
delta, a fast 1m price trend, a 1m order-flow persistence window, and session-end
flattening in the final 5%.

This iteration adds two risk-management rules:

1. **Post-loss cooldown.** When a position is closed for a loss, new entries are
   suppressed for the next few 1m bars. Momentum strategies often bleed through
   consecutive stop-outs; waiting a short while after a realized loss avoids
   re-entering the same whipsaw.

2. **Maximum holding period.** A position that neither reaches its target nor
   is stopped out within 60 bars is flattened. This cuts stale trades that have
   lost momentum and would otherwise sit through a long non-move until the
   session-end close, tying up the one position and accumulating small adverse
   drift.

The entry model is otherwise unchanged from the best prior trial, and the
trigger-bar flow-purity floor from an earlier trial is retained, so the engine
does as little new work as possible while tightening the exit side.
"""

from __future__ import annotations

from src.core.types import Side


class Strategy:
    """ATR / delta momentum with cooldown and max-hold exits."""

    def __init__(
        self,
        *,
        interval: str = "1m",
        lookback: int = 8,
        trend_window: int = 12,
        flow_window: int = 6,
        flow_min_frac: float = 0.5,
        entry_frac: float = 0.20,
        stop_mult: float = 1.5,
        target_mult: float = 3.0,
        trigger_purity: float = 0.10,
        session_margin_frac: float = 0.10,
        close_margin_frac: float = 0.05,
        cooldown_bars: int = 3,
        max_hold_bars: int = 60,
        size: int = 1,
    ):
        self.interval = interval
        self.lookback = lookback
        self.trend_window = trend_window
        self.flow_window = flow_window
        self.flow_min_frac = flow_min_frac
        self.entry_frac = entry_frac
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.trigger_purity = trigger_purity
        self.session_margin_frac = session_margin_frac
        self.close_margin_frac = close_margin_frac
        self.cooldown_bars = cooldown_bars
        self.max_hold_bars = max_hold_bars
        self.size = size

        # State for per-trade risk management.
        self._entry_price: float | None = None
        self._entry_side: Side | None = None
        self._bars_held: int = 0
        self._cooldown_left: int = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Manage an open position first. The bracket handles the protective
        # stop and target; these rules handle the cases between those.
        if ctx.position:
            self._bars_held += 1

            # Max holding period: flatten a stale position instead of letting
            # it sit until the session-end close.
            if self._bars_held >= self.max_hold_bars:
                ctx.close()
                self._bars_held = -100000
                return

            # Flatten before the session closes rather than holding through the
            # boundary.
            session_len = ctx.session_end - ctx.session_start
            if session_len > 0:
                remaining = ctx.session_end - ctx.ts
                if remaining < session_len * self.close_margin_frac:
                    ctx.close()
                    self._bars_held = -100000
                    return
            return

        # Flat. Do not re-enter immediately after a losing trade.
        if self._cooldown_left > 0:
            self._cooldown_left -= 1
            return

        # One position at a time; the bracket handles other exits.
        if ctx.working_order_ids:
            return

        # Do not take fresh risk in the last part of a session.
        session_len = ctx.session_end - ctx.session_start
        if session_len > 0:
            remaining = ctx.session_end - ctx.ts
            if remaining < session_len * self.session_margin_frac:
                return

        hist = ctx.history(self.interval)
        needed = max(
            self.lookback + 1,
            self.trend_window + 1,
            self.flow_window + 1,
        )
        if len(hist) < needed:
            return

        # ATR over the bars immediately preceding the trigger bar.
        recent = hist.last(self.lookback + 1)
        prior = recent[:-1]
        if len(prior) < 2:
            return

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

        # Fast price trend: close above/below the average of previous closes.
        prior_trend = hist.last(self.trend_window + 1)[:-1]
        if len(prior_trend) < self.trend_window:
            return
        trend_mean = sum(b.close for b in prior_trend) / self.trend_window
        fast_uptrend = last.close > trend_mean
        fast_downtrend = last.close < trend_mean

        # Short-term order-flow persistence from previous bars, excluding the
        # trigger bar itself.
        prior_flow = hist.last(self.flow_window + 1)[:-1]
        if len(prior_flow) < self.flow_window:
            return

        flow_delta = sum(b.delta for b in prior_flow)
        positive_flow_bars = sum(1 for b in prior_flow if b.delta > 0)
        negative_flow_bars = sum(1 for b in prior_flow if b.delta < 0)
        flow_min_bars = max(1, int(self.flow_window * self.flow_min_frac))

        # Trigger-bar flow purity: require a real imbalance, not merely a
        # positive sign on a near-tie bar.
        denom = max(1, last.volume)
        long_purity = last.delta / denom
        short_purity = -last.delta / denom

        stop = self.stop_mult * atr
        target = self.target_mult * atr

        if (
            body >= threshold
            and last.delta > 0
            and fast_uptrend
            and flow_delta > 0
            and positive_flow_bars >= flow_min_bars
            and long_purity >= self.trigger_purity
        ):
            ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="momentum-long",
            )
        elif (
            body <= -threshold
            and last.delta < 0
            and fast_downtrend
            and flow_delta < 0
            and negative_flow_bars >= flow_min_bars
            and short_purity >= self.trigger_purity
        ):
            ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="momentum-short",
            )

    def on_fill(self, ctx) -> None:
        fill = ctx.fill

        if self._entry_price is None:
            # Opening fill.
            if fill.position != 0:
                self._entry_price = fill.price
                self._entry_side = fill.side
                self._bars_held = 0
            return

        # Closing fill.
        if fill.position == 0:
            if self._entry_side is Side.BUY:
                pnl = fill.price - self._entry_price
            else:
                pnl = self._entry_price - fill.price

            if pnl < 0:
                self._cooldown_left = self.cooldown_bars

            self._entry_price = None
            self._entry_side = None
            self._bars_held = 0

    def on_lifecycle(self, ctx) -> None:
        event = ctx.event
        if getattr(event, "kind", None) == "session_open":
            self._cooldown_left = 0
            self._bars_held = 0
