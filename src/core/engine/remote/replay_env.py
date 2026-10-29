"""A bar replay a fitted strategy may train against. Harness-owned.

Loaded by file path inside the strategy process, as `harness_replay`, because
importing it through `src.core.engine` would load the engine into that process.
It imports nothing from this repository. A strategy passes the bars it already
holds; this module loads no data.

The fill rule is the bar form of `unfavorable`, and it is exact only for a
resting limit held for the whole bar and for a market order:

- a buy limit at `p` fills if and only if the bar's low is strictly below `p`
  (a sell limit: the high strictly above), at `p`;
- a market order fills at the next bar's open, which is the first trade after
  the decision.

A stop and a target inside one bar's range cannot be ordered from the bar.
This module does not model brackets. `docs/design/rl-strategies.md` section 2b.

The reward is one session's return as `session_returns` builds it: realised
P&L net of commission, plus the idle charge. The charge uses the declared
position cap rather than the peak position of the whole run, so one session's
reward does not depend on a different session. `IDLE_CHARGE` is copied here
and a test asserts it still equals `src.core.scoring.metrics.IDLE_CHARGE`.
Importing the metric from this module would put the scorer in the strategy
process, which is the thing this file exists to avoid.
"""

from __future__ import annotations

import math

# Pre-registration Amendment 3. Must match metrics.IDLE_CHARGE. The test
# `test_the_idle_charge_matches_the_metric` is what notices a drift.
IDLE_CHARGE = 0.18


class SessionReplay:
    """Replay one session of bars and return that session's reward.

    `policy(index, bar, position)` returns `None` or a tuple:

    - `("market", "buy" | "sell")`
    - `("limit", "buy" | "sell", price)`

    A decision taken on a bar is eligible on the next bar, and a new decision
    replaces one that is still resting. Size is one contract, capped by
    `position_cap`.
    """

    def __init__(self, *, commission: float, position_cap: int = 1,
                 idle_charge: float = IDLE_CHARGE, notional_base: float = 1.0):
        if position_cap < 1:
            raise ValueError("position_cap must be at least 1")
        if notional_base <= 0:
            raise ValueError("notional_base must be positive")
        self.commission = commission
        self.position_cap = position_cap
        self.idle_charge = idle_charge
        self.notional_base = notional_base
        self.last_fills: list[dict] = []
        self.last_span: tuple[int, int, int] | None = None

    def run_session(self, bars, policy) -> float:
        bars = list(bars)
        self.last_fills = []
        if not bars:
            self.last_span = None
            return 0.0
        start = int(bars[0].open_ts)
        end = int(bars[-1].close_ts)
        session = int(bars[0].session)
        self.last_span = (session, start, end)

        position = 0
        avg = 0.0
        pending = None
        opened_at: int | None = None
        held: list[tuple[int, int]] = []

        def fill(ts: int, side: str, price: float) -> None:
            nonlocal position, avg, opened_at
            sign = 1 if side == "buy" else -1
            if position != 0 and (position > 0) != (sign > 0):
                closing = min(1, abs(position))
                realized = (price - avg) * closing * (1 if position > 0 else -1)
                position += sign
                if position == 0 and opened_at is not None:
                    held.append((opened_at, ts))
                    opened_at = None
            else:
                if abs(position) >= self.position_cap:
                    return
                total = abs(position) + 1
                avg = (avg * abs(position) + price) / total
                realized = 0.0
                if position == 0:
                    opened_at = ts
                position += sign
            self.last_fills.append({
                "ts": ts,
                "session": session,
                "side": side,
                "price": price,
                "size": 1,
                "position_after": position,
                "commission": self.commission,
                "realized": realized,
            })

        for i, bar in enumerate(bars):
            if pending is not None:
                kind, side = pending[0], pending[1]
                if kind == "market":
                    fill(int(bar.open_ts), side, float(bar.open))
                    pending = None
                else:
                    price = float(pending[2])
                    hit = (
                        side == "buy" and float(bar.low) < price
                    ) or (
                        side == "sell" and float(bar.high) > price
                    )
                    if hit:
                        fill(int(bar.close_ts), side, price)
                        pending = None
            decision = policy(i, bar, position)
            if decision is not None:
                pending = decision

        if position != 0 and opened_at is not None:
            held.append((opened_at, end))

        pnl = math.fsum(
            row["realized"] - row["commission"] for row in self.last_fills
        )
        span = end - start
        covered = 0
        for lo, hi in held:
            a, b = max(lo, start), min(hi, end)
            if b > a:
                covered += b - a
        exposure = min(1.0, covered / span) if span > 0 else 0.0
        # The cap, not the peak of this session. A one-session blotter whose
        # peak equals the cap matches `idle_charges`; a larger cap does not,
        # and that is the point of fixing it up front.
        charge = -self.idle_charge * self.position_cap * (1.0 - exposure) / self.notional_base
        return pnl / self.notional_base + charge
