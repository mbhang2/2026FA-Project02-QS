"""Rebuild position, average price and P&L from the fill log alone.

A pure function of `(fills, commission_per_contract)`. No engine, no dataset, no
accumulated state -- which is the point: the score's source of truth has to be
something a strategy cannot reach, and the only way to be sure of that is for the
computation to take nothing that could have been reached.

The arithmetic is a re-derivation, not a redefinition. Average price moves only
when exposure increases, is left alone while reducing or flattening, and resets
at the fill that flips a position through zero; realized P&L is booked on the
closing portion at the price it closed at; commission is per contract on every
fill. Those are the rules `Engine._apply_fill` already followed, transcribed. If
the two ever disagree, that is a defect to name and not a tie to break.

The output is a `Blotter`, deliberately. Everything downstream -- the metric, the
session attribution, the digest -- already speaks that shape, so the change is
where the number comes from and nowhere else.
"""

from __future__ import annotations

from typing import Sequence

from ..types import Blotter, BlotterRow, FillRecord, Side


class FillLogFull(RuntimeError):
    """The run produced more fills than the log is allowed to hold.

    Loud on purpose. A truncated log is a wrong score with no error, which is the
    entire class of defect this module exists to remove -- so the bound raises
    rather than dropping the tail. A strategy sitting on the action cap for
    hundreds of sessions is the case that finds it, and it should find an
    exception.
    """


def reconstruct(
    fills: Sequence[FillRecord], *, commission_per_contract: float
) -> Blotter:
    """The blotter the fills imply.

    Ordered by `seq`, because the log is the record of what happened and the
    order it happened in is part of that. Sorting here rather than trusting
    append order costs nothing and removes a way for the two to differ.
    """
    blotter = Blotter()
    position = 0
    avg_price = 0.0
    last_price = 0.0

    for fill in sorted(fills, key=lambda f: (f.seq, f.order_id)):
        signed = fill.size * fill.side.sign
        realized = 0.0
        last_price = fill.price

        if position != 0 and (position > 0) != (signed > 0):
            closing = min(abs(signed), abs(position))
            realized = (fill.price - avg_price) * closing * (1 if position > 0 else -1)
            remaining_after = position + signed
            if not ((position > 0) == (remaining_after > 0) or remaining_after == 0):
                # flipped through zero: the new exposure was opened here
                avg_price = fill.price
            position = remaining_after
        else:
            total = abs(position) + fill.size
            avg_price = (
                (avg_price * abs(position) + fill.price * fill.size) / total
                if total
                else fill.price
            )
            position += signed

        blotter.add(
            BlotterRow(
                seq=fill.seq,
                ts=fill.ts,
                order_id=fill.order_id,
                side=fill.side,
                price=fill.price,
                size=fill.size,
                position_after=position,
                commission=commission_per_contract * fill.size,
                realized_pnl=realized,
                tag=fill.tag,
            )
        )

    blotter.final_position = position
    blotter.final_price = last_price
    return blotter


def log_digest(fills: Sequence[FillRecord]) -> str:
    """A hash of the log as it stands, for the run record.

    Recorded so replay can *verify* the P&L rather than trust it. The exploit
    that motivated this module reproduced perfectly under replay, because replay
    re-ran the same mutation and got the same wrong answer; a digest over the
    facts gives the replay something to check the derivation against.
    """
    import hashlib
    import json

    payload = json.dumps([f.as_row() for f in fills], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()
