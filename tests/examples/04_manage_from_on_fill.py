"""A resting limit entry, managed by hand: cancel, arm, trail, close.

Everything the bracket does for you in the earlier examples, done explicitly --
because sooner or later a rule does not fit a bracket. Note where each decision
lives: the entry is placed on a bar, the protection is armed on the *fill*, and
the exit is a bar decision again.

The gap this example is really about: between placing an entry and its fill you
are unprotected, and `on_fill` is the first instant you can do anything. A
bracket closes that gap inside the engine; managing it yourself does not.
"""


class Strategy:
    def __init__(self, interval="5m", size=1, offset=0.5, stop=1.0,
                 trail=0.5, patience=3, max_hold=8):
        self.interval = interval
        self.size = size
        self.offset = offset        # how far below the market to rest the entry
        self.stop = stop            # initial protective distance
        self.trail = trail          # tighten the stop by this much per bar held
        self.patience = patience    # bars to wait for the entry before pulling it
        self.max_hold = max_hold    # bars to hold before flattening
        self.entry_id = None
        self.stop_id = None
        self.entry_price = None
        self.age = 0

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable:
            return
        self.age += 1

        if ctx.position:
            self._manage_open_position(ctx)
            return

        if self.entry_id is not None:
            # Still waiting on the resting entry. Pull it if it has gone stale:
            # a limit that never fills is not free, it is an exposure to a
            # move you no longer have a view on.
            if self.age > self.patience:
                ctx.cancel(self.entry_id)
                self.entry_id = None
            return

        # Flat, nothing working: rest a new bid under the market.
        self.entry_id = ctx.buy(
            size=self.size,
            type="limit",
            limit=ctx.last_price - self.offset,
            tif="DAY",            # cancelled at the session close, not carried
            tag="limit-entry",
        )
        # buy() returns None when the action cap dropped the call. Treat that as
        # "no order exists" rather than assuming an id.
        self.age = 0

    def _manage_open_position(self, ctx):
        if self.age >= self.max_hold:
            if self.stop_id is not None:
                ctx.cancel(self.stop_id)   # cancel before flattening
                self.stop_id = None
            ctx.close()                    # market, reduce_only, tagged "close"
            return
        if self.stop_id is not None:
            # Trail the protective stop upward, never down.
            new_stop = ctx.last_price - self.stop + self.trail * self.age
            ctx.modify(self.stop_id, trigger=min(new_stop, ctx.last_price - 0.25))

    def on_fill(self, ctx):
        fill = ctx.fill
        if fill.order_id == self.entry_id:
            # The first instant protection can exist. reduce_only so it can
            # only ever close this position, never open a new one.
            self.entry_id = None
            self.entry_price = fill.price
            self.age = 0
            self.stop_id = ctx.sell(
                size=fill.size,
                type="stop",
                trigger=fill.price - self.stop,
                tif="GTC",         # survives the session close, like the position
                reduce_only=True,
                tag="protective-stop",
            )
        elif fill.order_id == self.stop_id or fill.position == 0:
            self.stop_id = None
            self.entry_price = None
            self.age = 0

    def on_lifecycle(self, ctx):
        ev = ctx.event
        # A DAY order dies at the session close. If the entry went with it, the
        # id is stale and reusing it would raise on the next cancel().
        if ev.order_id is not None and ev.order_id == self.entry_id:
            if str(ev.kind) in ("day_expired", "gtd_expired", "cancel_confirmed",
                                "oco_cancelled", "order_rejected"):
                self.entry_id = None
        if ev.order_id is not None and ev.order_id == self.stop_id:
            if str(ev.kind) in ("day_expired", "gtd_expired", "cancel_confirmed",
                                "oco_cancelled", "order_rejected"):
                self.stop_id = None
