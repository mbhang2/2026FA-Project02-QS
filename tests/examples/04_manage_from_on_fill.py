"""A resting limit entry, managed by hand: cancel, arm, trail, close.

Everything the bracket does for you in the earlier examples, done explicitly --
because sooner or later a rule does not fit a bracket. Note where each decision
lives: the entry is placed on a bar, the protection is armed on the *fill*, and
the exit is a bar decision again.

The gap this example is really about: between placing an entry and its fill you
are unprotected, and `on_fill` is the first instant you can do anything. A
bracket closes that gap inside the engine; managing it yourself does not.

Orders are found by **tag** rather than by an id kept on `self`. A stored id goes
stale the moment the order expires -- a DAY order dies at the session close --
and acting on a stale id raises. `ctx.find_orders(tag=...)` asks the engine what
is working right now, so there is no stale state to get wrong.
"""

ENTRY = "limit-entry"
STOP = "protective-stop"


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

        resting = ctx.find_orders(tag=ENTRY)
        if resting:
            # Still waiting on the entry. Pull it if it has gone stale: a limit
            # that never fills is not free, it is an exposure to a move you no
            # longer have a view on.
            if self.age > self.patience:
                ctx.cancel(resting[0].id)
            return

        # Flat, nothing working: rest a new bid under the market.
        ctx.buy(
            size=self.size,
            type="limit",
            limit=ctx.last_price - self.offset,
            tif="DAY",            # cancelled at the session close, not carried
            tag=ENTRY,
        )
        self.age = 0

    def _manage_open_position(self, ctx):
        stops = ctx.find_orders(tag=STOP)
        if self.age >= self.max_hold:
            for order in stops:
                ctx.cancel(order.id)     # cancel before flattening
            ctx.close()                  # market, reduce_only, tagged "close"
            return
        if stops:
            # Trail the protective stop upward, never down.
            new_stop = ctx.last_price - self.stop + self.trail * self.age
            ctx.modify(stops[0].id, trigger=min(new_stop, ctx.last_price - 0.25))

    def on_fill(self, ctx):
        fill = ctx.fill
        if fill.position == 0:
            self.age = 0
            return
        if not ctx.find_orders(tag=STOP):
            # The first instant protection can exist. reduce_only so it can only
            # ever close this position, never open a new one.
            self.age = 0
            ctx.sell(
                size=abs(fill.position),
                type="stop",
                trigger=fill.price - self.stop,
                tif="GTC",         # survives the session close, like the position
                reduce_only=True,
                tag=STOP,
            )

    def on_lifecycle(self, ctx):
        # Nothing to clean up: no order id is kept across callbacks, so a DAY
        # expiry at the session close cannot leave this strategy holding one.
        pass
