"""Trade continuation after a session-relative directional move."""


class Strategy:
    def __init__(
        self,
        interval="5m",
        size=1,
        min_bars=6,
        signal_multiple=3.0,
        min_efficiency=0.55,
        first_entry_fraction=0.20,
        last_entry_fraction=0.75,
        exit_buffer_minutes=10,
        stop_multiple=1.5,
        target_multiple=3.0,
    ):
        self.interval = interval
        self.size = size
        self.min_bars = min_bars
        self.signal_multiple = signal_multiple
        self.min_efficiency = min_efficiency
        self.first_entry_fraction = first_entry_fraction
        self.last_entry_fraction = last_entry_fraction
        self.exit_buffer_ns = exit_buffer_minutes * 60 * 1_000_000_000
        self.stop_multiple = stop_multiple
        self.target_multiple = target_multiple

        self._session = None
        self._opening_price = None
        self._previous_close = None
        self._path = 0.0
        self._bar_count = 0
        self._traded_this_session = False
        self._exit_requested = False

    def setup(self, ctx):
        ctx.subscribe(self.interval, emit_empty=True)

    def on_bars(self, ctx):
        if self.interval not in ctx.bars:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        bar = history[-1]
        if ctx.session != self._session:
            self._session = ctx.session
            self._opening_price = bar.open
            self._previous_close = bar.open
            self._path = 0.0
            self._bar_count = 0
            self._traded_this_session = False
            self._exit_requested = False

        self._path += abs(bar.close - self._previous_close)
        self._previous_close = bar.close
        self._bar_count += 1

        if ctx.position:
            if (
                not self._exit_requested
                and ctx.session_end - ctx.ts <= self.exit_buffer_ns
            ):
                ctx.cancel_all()
                close_id = ctx.close()
                if close_id is not None:
                    self._exit_requested = True
            return

        if not ctx.actionable or ctx.working_order_ids:
            return
        if self._traded_this_session or self._bar_count < self.min_bars:
            return

        session_length = ctx.session_end - ctx.session_start
        if session_length <= 0:
            return
        elapsed_fraction = (ctx.ts - ctx.session_start) / session_length
        if (
            elapsed_fraction < self.first_entry_fraction
            or elapsed_fraction > self.last_entry_fraction
            or ctx.session_end - ctx.ts <= self.exit_buffer_ns
        ):
            return

        average_move = self._path / self._bar_count
        if average_move <= 0:
            return

        displacement = bar.close - self._opening_price
        standardized_move = abs(displacement) / average_move
        efficiency = abs(displacement) / self._path if self._path > 0 else 0.0
        if (
            standardized_move < self.signal_multiple
            or efficiency < self.min_efficiency
        ):
            return

        stop = average_move * self.stop_multiple
        target = average_move * self.target_multiple
        if displacement > 0:
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="session-move-continuation-long",
            )
        else:
            order_id = ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="session-move-continuation-short",
            )
        if order_id is not None:
            self._traded_this_session = True

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
