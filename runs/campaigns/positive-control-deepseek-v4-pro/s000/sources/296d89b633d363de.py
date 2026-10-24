"""One position, re-evaluated once per session on a fitted early-bar signal.

Prior trials show that turnover is the strategy killer in this harness, while
the best results came from keeping exposure nearly continuous with very few
fills.  This strategy combines those observations:

1. It subscribes only to 5-minute bars (bar intervals finish well within the
   wall-clock budget; a tick subscription does not).
2. In setup() it fits a small session-level predictor from the training bars:
   it asks which early-session return `close_of_bar_k - open_of_first_bar`
   (for k = 1..5) best predicts the remaining session return, and whether the
   relationship is momentum or reversal.
3. At runtime it waits for that fitted lookback to close, computes the same
   early-session return, and then makes one desired position (+1 or -1) for
   that session.  If the current position already matches, it does nothing;
   if the other side is desired, it flips in a single market order.  This
   gives continuous exposure with low turnover: one initial fill plus one
   fill only on the rare sessions where the desired side changes.

No stops or targets are used, because every extra fill has been costly in
previous trials.  The position is held across sessions; changing the desired
side closes the old position and opens the new one in one order.
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Fitted early-session direction, held with minimal turnover."""

    def __init__(self, *, interval: str = "5m", size: int = 1,
                 max_lookback: int = 5) -> None:
        self.interval = interval
        self.size = size
        self.max_lookback = max_lookback

        # Fitted in setup()
        self._lookback = 1
        self._sign = 1   # +1: same direction as early return, -1: opposite
        self._min_edge = 0.0

        # Session state
        self._session = None
        self._bars_this_session = 0
        self._session_bars: list[Any] = []
        self._evaluated_this_session = False
        self._desired = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        self._fit(ctx)

    def _fit(self, ctx) -> None:
        """Fit lookback and direction from the training split available in setup."""
        try:
            bars = ctx.train_bars(self.interval)
        except Exception:
            bars = ()

        if not bars:
            # Default: first-bar momentum, same direction.
            self._lookback = 1
            self._sign = 1
            return

        sessions: dict[int, list[Any]] = {}
        for b in bars:
            sessions.setdefault(b.session, []).append(b)

        best_k = 1
        best_sign = 1
        best_edge = -1.0

        for k in range(1, self.max_lookback + 1):
            pos_rets = []
            neg_rets = []

            for sb in sessions.values():
                if len(sb) <= k:
                    continue

                early_ret = sb[k - 1].close - sb[0].open
                rest_ret = sb[-1].close - sb[k - 1].close

                if early_ret > 0:
                    pos_rets.append(rest_ret)
                elif early_ret < 0:
                    neg_rets.append(rest_ret)

            if len(pos_rets) < 15 or len(neg_rets) < 15:
                continue

            pos_mean = sum(pos_rets) / len(pos_rets)
            neg_mean = sum(neg_rets) / len(neg_rets)
            diff = pos_mean - neg_mean
            edge = abs(diff)

            if edge > best_edge:
                best_edge = edge
                best_k = k
                best_sign = 1 if diff > 0 else -1

        self._lookback = best_k
        self._sign = best_sign

        # If the fitted relationship is extremely weak, fall back to the overall
        # training drift so the strategy still has a defensible direction while
        # maintaining exposure.
        if best_edge <= self._min_edge:
            drift = bars[-1].close - bars[0].close
            self._lookback = 1
            self._sign = 1 if drift > 0 else -1

    def _direction_for_early_ret(self, early_ret: float) -> int:
        """Return desired position +1 or -1 for a non-zero early return."""
        if early_ret > 0:
            return self._sign
        return -self._sign

    def _apply_desired(self, ctx, desired: int) -> None:
        """Queue the order needed to move from the current position to desired."""
        pos = ctx.position

        if desired == 0:
            if pos != 0:
                ctx.close()
            return

        if pos == 0:
            if desired > 0:
                ctx.buy(size=self.size, tif="DAY", tag="session-long")
            else:
                ctx.sell(size=self.size, tif="DAY", tag="session-short")
            return

        if pos > 0:
            if desired > 0:
                return
            # Flip from long to short in one market order.
            ctx.sell(size=pos + self.size, tif="DAY", tag="session-flip-short")
        else:
            if desired < 0:
                return
            # Flip from short to long in one market order.
            ctx.buy(size=-pos + self.size, tif="DAY", tag="session-flip-long")

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return

        bar = ctx.bars[self.interval]

        # Session changed: reset the per-session counter and buffer.
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_this_session = 0
            self._session_bars = []
            self._evaluated_this_session = False

        self._bars_this_session += 1
        self._session_bars.append(bar)

        if not ctx.actionable:
            return

        if ctx.working_order_ids:
            return

        if self._evaluated_this_session or self._bars_this_session < self._lookback:
            return

        # The fitted entry point has closed.  Wait for a non-zero early return
        # rather than guessing on a flat open-to-close move.
        early_ret = bar.close - self._session_bars[0].open
        if abs(early_ret) < 1e-12:
            return

        desired = self._direction_for_early_ret(early_ret)
        self._desired = desired
        self._evaluated_this_session = True
        self._apply_desired(ctx, desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
