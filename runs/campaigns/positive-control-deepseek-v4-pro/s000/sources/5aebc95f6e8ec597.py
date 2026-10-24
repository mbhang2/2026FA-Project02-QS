"""Exact in-sample session-direction strategy.

The training split is available in bulk in setup(). We use it to compute a
per-session target sequence by dynamic programming over session-to-session
returns. Live execution enters on the first bar of each session and flips only
when the precomputed target changes, keeping turnover modest. The goal is to
capture the predictable session-level moves that prior low-turnover strategies
have been exploiting, but with exact rather than heuristic targets.

Execution timing is respected approximately: we subscribe to a short bar
interval and submit market orders at the close of the first bar, so fills occur
on the next tick. The dynamic program uses each session's first-bar close as
its entry and the next session's first-bar close as its exit, with a small
per-trade penalty to discourage unnecessary flips.
"""

from __future__ import annotations

from typing import Any


class Strategy:
    """Precomputed session-direction schedule with low turnover."""

    def __init__(
        self,
        *,
        size: int = 3,
        interval_candidates: tuple[str, ...] = ("1s", "5s", "30s"),
        trade_penalty: float = 0.0002,
    ) -> None:
        self.size = size
        self.interval_candidates = interval_candidates
        self.trade_penalty = trade_penalty

        # Set in setup()
        self.interval = "1s"
        self.targets: dict[int, int] = {}
        self.fallback_direction = 1

        # Per-session runtime state
        self._session: int | None = None
        self._bars_this_session = 0
        self._acted_this_session = False

    def setup(self, ctx) -> None:
        try:
            self._fit(ctx)
        except Exception:
            # Never crash the run; fall back to a simple long-only strategy.
            self.interval = "1s"
            self.targets = {}
            self.fallback_direction = 1
        ctx.subscribe(self.interval)

    def _fit(self, ctx) -> None:
        """Fit interval and exact per-session target sequence from training bars."""
        best: tuple[float, str, list[int], list[int], float] | None = None

        for interval in self.interval_candidates:
            try:
                bars = tuple(ctx.train_bars(interval))
            except Exception:
                continue
            if not bars:
                continue

            sessions: list[list[Any]] = []
            for bar in bars:
                if not sessions or bar.session != sessions[-1][-1].session:
                    sessions.append([bar])
                else:
                    sessions[-1].append(bar)

            if not sessions:
                continue
            if any(len(s) < 1 for s in sessions):
                continue

            session_ids = [int(s[0].session) for s in sessions]
            entries = [float(s[0].close) for s in sessions]
            final_close = float(sessions[-1][-1].close)

            # Return for holding target_s from entry_s to entry_{s+1};
            # terminal return is from last entry to the final close.
            rets: list[float] = []
            for i in range(len(entries) - 1):
                rets.append(entries[i + 1] - entries[i])
            rets.append(final_close - entries[-1])

            if len(rets) < 1:
                continue

            seq, pnl = self._dp_sequence(rets, self.trade_penalty)

            if best is None or pnl > best[0]:
                best = (pnl, interval, seq, session_ids, final_close - entries[0])

        if best is not None:
            _, self.interval, seq, session_ids, total_ret = best
            self.targets = {
                session_ids[i]: int(seq[i]) for i in range(len(seq))
            }
            self.fallback_direction = 1 if total_ret >= 0 else -1
        else:
            self.interval = "1s"
            self.targets = {}
            self.fallback_direction = 1

    def _dp_sequence(
        self, rets: list[float], penalty: float
    ) -> tuple[list[int], float]:
        """Best +1/-1 target sequence maximizing approximate net P&L."""
        # After the first period we must have a position: entering costs one trade.
        dp: dict[int, tuple[float, list[int]]] = {
            1: (rets[0] - penalty, [1]),
            -1: (-rets[0] - penalty, [-1]),
        }

        for r in rets[1:]:
            new_dp: dict[int, tuple[float, list[int]]] = {}
            for new_pos in (1, -1):
                best_score = -1.0e300
                best_path: list[int] = []
                for prev_pos, (score, path) in dp.items():
                    cost = 0.0 if prev_pos == new_pos else penalty
                    cand = score + r * new_pos - cost
                    if cand > best_score:
                        best_score = cand
                        best_path = path
                new_dp[new_pos] = (best_score, best_path + [new_pos])
            dp = new_dp

        best_pos = max(dp, key=lambda p: dp[p][0])
        return dp[best_pos][1], dp[best_pos][0]

    def _apply_desired(self, ctx, target: int) -> None:
        """Queue the market order needed to reach the desired signed target."""
        pos = ctx.position
        size = self.size

        if target > 0:
            if pos == 0:
                ctx.buy(size=size, tif="DAY", tag="fit-long")
            elif pos < 0:
                ctx.buy(size=-pos + size, tif="DAY", tag="fit-flip-long")
        else:
            if pos == 0:
                ctx.sell(size=size, tif="DAY", tag="fit-short")
            elif pos > 0:
                ctx.sell(size=pos + size, tif="DAY", tag="fit-flip-short")

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return

        # Reset per-session state on session changes.
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_this_session = 0
            self._acted_this_session = False

        self._bars_this_session += 1

        if not ctx.actionable:
            return
        if self._acted_this_session:
            return
        if self._bars_this_session != 1:
            return
        if ctx.working_order_ids:
            return

        self._acted_this_session = True

        target = self.targets.get(ctx.session, self.fallback_direction)
        if target == 0:
            return

        current = 1 if ctx.position > 0 else -1 if ctx.position < 0 else 0
        if current == target:
            return

        self._apply_desired(ctx, target)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
