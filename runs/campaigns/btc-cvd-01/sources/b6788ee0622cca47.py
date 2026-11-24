"""CVD continuation on the OKX BTCUSDT perpetual.

Claim under test (from the campaign brief): when aggressive buying outweighs
aggressive selling over a window, price keeps rising over the next comparable
window, and when aggressive selling outweighs buying, price keeps falling. That
is a *continuation* claim, so every entry is with the flow and none against it.

Signal
------
On every closed 5m bar, take the last `flow_window` (12) bars -- one hour -- and
form the volume-normalised cumulative delta

    ratio = sum(bar.delta) / sum(bar.volume)        in [-1, 1]

`bar.delta` is `buy_volume - sell_volume`, accumulated per tick by the bar
builder, so no tick subscription is needed. On this dataset every trade carries
an aggressor and `unknown_volume` is zero, so the ratio uses all of the volume
and no side is silently dropped.

The normalisation is the answer to "what is large". A hundred BTC of delta means
different things in a quiet Asian morning and a busy US open, and the feed's own
total volume over the same window is the scale that is always available and
always contemporaneous. On top of that an EWMA (alpha 0.02, roughly a 50-bar
memory) of |ratio| tracks how imbalanced flow normally is right now, and the
trigger is

    max(abs_floor, mult * ewma_abs)

The floor stops a dead market generating signals off noise; the multiplier stops
a genuinely choppy regime generating dozens of them. Nothing here is fitted from
a training split -- it is an online statistic the strategy could compute live.

Horizon
-------
The claim names a horizon, so this strategy names one: one hour. Hold for
`hold_bars` = 12 five-minute bars and then leave, whatever the P&L. The bracket
is protection and profit-taking, not the thesis -- it is scaled by ATR so the
same rule means the same thing at $20k and at $110k.

Entry and exit
--------------
Market orders both ways. The claim is about what price does *after* flow
arrives, so the entry has to happen at the market the flow implies. A resting
bid would only fill when price comes back, which selects exactly the cases where
continuation failed -- a biased test of the claim. The exit is the time exit
(market) or the bracket's stop/target.

Costs
-----
5bp a side, so 10bp a round trip, plus funding if a position straddles
00:00/08:00/16:00 UTC. A 12-bar hold at 5m is a 60-minute exposure and the
trigger is selective, so turnover lands around a dozen round trips a session:
comfortably under the 64-action cap and, more to the point, under the turnover
where the fee eats whatever edge exists. Entries are refused in the last hour of
a session because a DAY bracket leg dies at the UTC-day boundary and the
position would otherwise run naked.

Housekeeping. Bracket legs are cancelled before the time exit, so no reduce-only
leg survives into the next position. Orders are found through `ctx.working_orders`
and never through an id kept on `self`, because a DAY leg expires at the session
close and a stored id would then be stale. Every `history()` read goes through
`.last(n)`, which returns what exists instead of raising.

What is deliberately absent: no book, no quotes, no regime model, no fade. If
continuation is not there, this strategy loses money, and the campaign should
record that rather than quietly rotate to the other side.
"""

from __future__ import annotations

_NS_PER_MS = 10 ** 6
_NS_PER_S = 10 ** 9


def _interval_ns(text) -> int:
    """Nanoseconds for an interval spelling, e.g. '5m'. Falls back to 5m."""
    if isinstance(text, int):
        return int(text)
    units = (("ms", _NS_PER_MS), ("s", _NS_PER_S), ("m", 60 * _NS_PER_S),
             ("h", 3600 * _NS_PER_S), ("d", 86400 * _NS_PER_S))
    for suffix, scale in units:
        if text.endswith(suffix):
            try:
                return int(float(text[: -len(suffix)]) * scale)
            except ValueError:
                return 300 * _NS_PER_S
    return 300 * _NS_PER_S


class Strategy:
    def __init__(self, interval: str = "5m", flow_window: int = 12,
                 vol_window: int = 20, abs_floor: float = 0.20,
                 mult: float = 1.75, ewma_alpha: float = 0.02,
                 size: int = 1, hold_bars: int = 12, stop_atr: float = 2.0,
                 target_atr: float = 3.0, cooldown: int = 12,
                 warmup_bars: int = 40):
        self.interval = interval
        self.flow_window = flow_window
        self.vol_window = vol_window
        self.abs_floor = abs_floor
        self.mult = mult
        self.ewma_alpha = ewma_alpha
        self.size = size
        self.hold_bars = hold_bars
        self.stop_atr = stop_atr
        self.target_atr = target_atr
        self.cooldown = cooldown
        self.warmup_bars = warmup_bars

        # How many bars of history one decision needs: the flow window and the
        # ATR window (which needs one extra bar to form a true range).
        self.need = max(flow_window, vol_window + 1)
        self.interval_ns = _interval_ns(interval)
        self.hold_ns = hold_bars * self.interval_ns

        self.ewma_abs: float | None = None
        self.seen = 0
        self.held_bars = 0
        self.bars_since_exit = 10 ** 6

    # ------------------------------------------------------------------ setup
    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    # ---------------------------------------------------------------- signals
    def _update_ewma(self, value: float) -> None:
        if self.ewma_abs is None:
            self.ewma_abs = value
        else:
            self.ewma_abs += self.ewma_alpha * (value - self.ewma_abs)

    def _flatten(self, ctx) -> None:
        """Cancel the bracket, then leave at the market. Order matters."""
        for order in ctx.working_orders:
            ctx.cancel(order.id)
        if ctx.position != 0:
            ctx.close()
        self.held_bars = 0
        self.bars_since_exit = 0

    # --------------------------------------------------------------- callback
    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return

        recent = ctx.history(self.interval).last(self.need)
        if len(recent) < self.need:
            return                      # legitimate: not enough bars yet

        self.seen += 1
        self.bars_since_exit += 1

        # --- cumulative delta over the flow window, normalised by its volume
        window = recent[-self.flow_window:]
        total_volume = 0
        total_delta = 0
        for bar in window:
            total_volume += bar.volume
            total_delta += bar.delta
        if total_volume <= 0:
            return
        ratio = total_delta / float(total_volume)

        # --- ATR over the same bars, used to scale the bracket
        tr_sum = 0.0
        for i in range(1, len(recent)):
            bar = recent[i]
            prev = recent[i - 1]
            span = bar.high - bar.low
            up = bar.high - prev.close
            if up < 0.0:
                up = -up
            down = prev.close - bar.low
            if down < 0.0:
                down = -down
            if up > span:
                span = up
            if down > span:
                span = down
            tr_sum += span
        atr = tr_sum / float(len(recent) - 1)
        if atr <= 0.0:
            return

        # --- holding: count bars, leave at the horizon
        if ctx.position != 0:
            self.held_bars += 1
            self._update_ewma(abs(ratio))
            if self.held_bars >= self.hold_bars:
                self._flatten(ctx)
            return

        self.held_bars = 0

        # --- anything still resting while flat is stale protection
        if ctx.working_order_ids:
            ctx.cancel_all()
            self._update_ewma(abs(ratio))
            return

        if self.bars_since_exit < self.cooldown:
            self._update_ewma(abs(ratio))
            return

        if self.seen < self.warmup_bars or not ctx.actionable:
            self._update_ewma(abs(ratio))
            return

        # Leave room to be flat before the UTC-day boundary: a DAY bracket leg
        # dies at the session close, and a position held through it is naked.
        if ctx.session_end - ctx.ts < self.hold_ns + 600 * _NS_PER_S:
            self._update_ewma(abs(ratio))
            return

        threshold = self.abs_floor
        if self.ewma_abs is not None:
            adaptive = self.mult * self.ewma_abs
            if adaptive > threshold:
                threshold = adaptive

        self._update_ewma(abs(ratio))

        stop_distance = self.stop_atr * atr
        target_distance = self.target_atr * atr
        if stop_distance <= 0.0 or target_distance <= 0.0:
            return

        if ratio >= threshold:
            order_id = ctx.buy(size=self.size,
                               stop_offset=stop_distance,
                               target_offset=target_distance,
                               tag="cvd-long")
            if order_id is not None:
                self.held_bars = 0
        elif ratio <= -threshold:
            order_id = ctx.sell(size=self.size,
                                stop_offset=stop_distance,
                                target_offset=target_distance,
                                tag="cvd-short")
            if order_id is not None:
                self.held_bars = 0

    # ------------------------------------------------------------------ fills
    def on_fill(self, ctx) -> None:
        # A flat book means a bracket leg took us out; start the cooldown so the
        # same imbalance is not re-bought on the very next bar.
        if ctx.fill.position == 0:
            self.held_bars = 0
            self.bars_since_exit = 0

    # -------------------------------------------------------------- lifecycle
    def on_lifecycle(self, ctx) -> None:
        # No order id is kept across callbacks, so a DAY expiry at the session
        # close cannot leave this strategy holding a stale one.
        pass
