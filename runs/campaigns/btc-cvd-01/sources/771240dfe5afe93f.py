"""Cumulative volume delta continuation on the OKX BTC perpetual.

The campaign claim: unusually large aggressive buying on the BTC perpetual is
followed by higher prices over the next comparable window, and unusually large
aggressive selling by lower prices. This is a *continuation* strategy: it trades
with the flow, never against it.

How the claim is turned into a rule
-----------------------------------
Each 15m bar carries its parts: ``bar.delta`` = ``buy_volume - sell_volume`` and
``bar.volume``. Over a window of ``flow_window`` bars I sum both, and the window's
flow score is

    S = sum(delta) / sum(volume)

which is the volume-weighted aggressive-buy share minus the aggressive-sell
share, in [-1, 1]. Dividing by volume is the normalisation the brief asks for:
BTC volume differs by an order of magnitude across hours and years, and a fixed
contract-count threshold would mean something different in every regime. The
ratio is scale free.

"Large" is then measured against the strategy's own recent experience of S.
S is appended to a rolling buffer of the last ``norm_window`` bars (24h at 15m)
and standardised:

    z = (S - mean(recent S)) / sd(recent S)

This is what makes the threshold portable across volatility regimes: on a quiet
day a modest imbalance is already unusual, on a busy day it is not. The buffer
is filled during warmup as well as during scoring, so the standardisation is
available from the first scored bar.

The horizon is named and held fixed: enter on the bar close where |z| crosses
``z_entry``, hold for ``hold_bars`` (2 hours) and exit on time. A time exit is
the honest test of "the move continues over the next comparable window" -- it
does not let a discretionary rule harvest the good cases and cut the bad ones.

Costs, and why the parameters are what they are
-----------------------------------------------
A round trip costs 10 bp (5 bp per side) plus the spread, and fills are
book-queue-pessimistic, so every entry and exit pays. That forces the horizon:
at 15m the per-bar move is far below the round-trip cost, so a signal has to be
allowed to pay over hours, not minutes. A 2-hour hold on a 3-hour flow window is
the "comparable window" the brief describes, and roughly five round trips a day
at most keeps the cost drag near 0.5%/day against BTC's ~2-3% daily range.

Fills use a bracket given as *offsets* of the fill price (never absolute levels,
which can invert against a slipped market fill): a 1.2% protective stop as a
disaster brake and a 3% target that is deliberately far away so the time exit,
not the target, is what normally closes the trade. Both legs are reduce-only OCO
inside the engine, so protection exists from the instant the entry fills rather
than from the next callback, and the sibling can never resolve against the same
trade.

No order ids are stored: the exit is ``cancel_all()`` + ``close()``, which cannot
go stale at a session boundary. A short cooldown after any exit stops the
strategy from re-entering the same, still-extreme, overlapping window.

The feed attributes every trade (unknown_volume is zero on this dataset), so
``bar.delta`` needs no unknown-side branch and no tick subscription -- which
matters, because a tick subscription costs roughly 5x and bar.delta is built by
the bar builder from the same trades.

Everything read from ``ctx.history`` is ``history[-1]`` after a length check, and
nothing recomputes over more than the two small deques it maintains.
"""

from __future__ import annotations

from collections import deque
from typing import Tuple


class Strategy:
    """Follow large, standardised cumulative volume delta; exit on time."""

    def __init__(
        self,
        interval: str = "15m",
        flow_window: int = 12,
        norm_window: int = 96,
        min_samples: int = 48,
        z_entry: float = 2.5,
        hold_bars: int = 8,
        cooldown_bars: int = 2,
        stop_pct: float = 0.012,
        target_pct: float = 0.030,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.flow_window = flow_window
        self.norm_window = norm_window
        self.min_samples = min_samples
        self.z_entry = z_entry
        self.hold_bars = hold_bars
        self.cooldown_bars = cooldown_bars
        self.stop_pct = stop_pct
        self.target_pct = target_pct
        self.size = size

        # Feature state, carried on self: the engine's history is a bounded
        # window and everything here is O(1) to update per bar.
        self.deltas: "deque[float]" = deque(maxlen=flow_window)
        self.volumes: "deque[float]" = deque(maxlen=flow_window)
        self.scores: "deque[float]" = deque(maxlen=norm_window)

        # Trade state.
        self.bars_held: int = 0
        self.cooldown: int = 0
        self.in_position: bool = False
        self.closing: bool = False

    # ------------------------------------------------------------------ setup

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    # ------------------------------------------------------------------- bars

    def on_bars(self, ctx) -> None:
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        bar = history[-1]          # the bar that just closed; never index past it

        # Accumulate on every bar, warmup included: the standardisation needs a
        # filled buffer before the first scored decision.
        self.deltas.append(float(bar.delta))
        self.volumes.append(float(bar.volume))

        score = None
        if len(self.deltas) >= self.flow_window:
            total_volume = 0.0
            for v in self.volumes:
                total_volume += v
            if total_volume > 0.0:
                total_delta = 0.0
                for d in self.deltas:
                    total_delta += d
                score = total_delta / total_volume
                self.scores.append(score)

        if not ctx.actionable:
            return

        # ---- carry a position: the only exit that is ours is the time exit.
        if ctx.position != 0:
            self.in_position = True
            if self.closing:
                return             # a close is already in flight
            self.bars_held += 1
            if self.bars_held >= self.hold_bars:
                self._flatten(ctx)
            return

        # ---- flat. If we were carrying something last bar it left through the
        # bracket; rest briefly so an overlapping window cannot re-fire at once.
        if self.in_position or self.closing:
            self.in_position = False
            self.closing = False
            self.bars_held = 0
            self.cooldown = self.cooldown_bars

        if self.cooldown > 0:
            self.cooldown -= 1
            return

        # An entry that has not filled yet, or a bracket leg still resting.
        if ctx.working_order_ids:
            return

        if score is None or len(self.scores) < self.min_samples:
            return

        mean, sd = self._stats()
        if sd <= 0.0:
            return
        z = (score - mean) / sd

        if z >= self.z_entry:
            self._enter(ctx, 1)
        elif z <= -self.z_entry:
            self._enter(ctx, -1)

    # ---------------------------------------------------------------- helpers

    def _stats(self) -> "Tuple[float, float]":
        n = len(self.scores)
        total = 0.0
        for v in self.scores:
            total += v
        mean = total / n
        var = 0.0
        for v in self.scores:
            d = v - mean
            var += d * d
        var /= float(n - 1) if n > 1 else 1.0
        return mean, var ** 0.5

    def _enter(self, ctx, direction: int) -> None:
        price = ctx.last_price
        if price is None or price <= 0.0:
            return
        # Positive distances measured from the fill, so a slipped market fill
        # can never put a bracket leg on the wrong side of the entry.
        stop = price * self.stop_pct
        target = price * self.target_pct
        order_id = None
        if direction > 0:
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="cvd-long",
            )
        else:
            order_id = ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="cvd-short",
            )
        if order_id is None:
            return             # the action cap dropped it; no order exists
        self.bars_held = 0
        self.in_position = True

    def _flatten(self, ctx) -> None:
        # Cancel first so the OCO legs cannot resolve against the exit, then
        # close. Everything is by tag-free bulk cancel: no stored id to go stale.
        ctx.cancel_all()
        if ctx.position != 0:
            ctx.close()
        self.bars_held = 0
        self.closing = True
        self.cooldown = self.cooldown_bars

    # -------------------------------------------------------------- callbacks

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
