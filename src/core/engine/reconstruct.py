"""Rebuild position, average price and P&L from the fill log alone.

A pure function of `(fills, commission_per_contract)`. No engine, no dataset, no
accumulated state -- which is the point: the score's source of truth has to be
something a strategy cannot reach, and the only way to be sure of that is for the
computation to take nothing that could have been reached.

The arithmetic is a re-derivation, not a redefinition. Average price moves only
when exposure increases, is left alone while reducing or flattening, and resets
at the fill that flips a position through zero; realized P&L is booked on the
closing portion at the price it closed at; commission is per contract on every
fill, plus `commission_bps` of the fill's notional where a profile sets it. Those are the rules `Engine._apply_fill` already followed, transcribed. If
the two ever disagree, that is a defect to name and not a tie to break.

The output is a `Blotter`, deliberately. Everything downstream -- the metric, the
session attribution, the digest -- already speaks that shape, so the change is
where the number comes from and nowhere else.
"""

from __future__ import annotations

from typing import Sequence

from ..types import DEFAULT_ASSET, Blotter, BlotterRow, FillRecord, Side


class FillLogFull(RuntimeError):
    """The run produced more fills than the log is allowed to hold.

    Loud on purpose. A truncated log is a wrong score with no error, which is the
    entire class of defect this module exists to remove -- so the bound raises
    rather than dropping the tail. A strategy sitting on the action cap for
    hundreds of sessions is the case that finds it, and it should find an
    exception.
    """


def fill_commission(price: float, size: int, *, commission_per_contract: float,
                    commission_bps: float = 0.0, slippage_per_contract: float = 0.0,
                    slippage_bps: float = 0.0, liquidity: str = "taker",
                    maker_commission_per_contract: float | None = None,
                    maker_commission_bps: float | None = None) -> float:
    """What one fill costs, in price units x size: a fixed amount per contract
    (NQ, in index points) plus a share of notional (crypto, in basis points of
    `price x size`). The engine and this module both charge through here, so the
    accumulator and the reconstruction cannot price a fill two ways.

    A maker fill pays the maker rates where they are set, and the same rates otherwise.
    A taker fill also pays slippage (Amendment 19). It is a cost and not a moved
    price, so the sign-flip null charges it again rather than flipping it."""
    per, bps = commission_per_contract, commission_bps
    if liquidity == "maker":
        per = per if maker_commission_per_contract is None else maker_commission_per_contract
        bps = bps if maker_commission_bps is None else maker_commission_bps
    cost = per * size + bps * 1e-4 * price * size
    if liquidity == "taker" and (slippage_per_contract or slippage_bps):
        cost += slippage_per_contract * size + slippage_bps * 1e-4 * price * size
    return cost


def reconstruct(
    fills: Sequence[FillRecord], *, commission_per_contract: float,
    commission_bps: float = 0.0, slippage_per_contract: float = 0.0,
    slippage_bps: float = 0.0, maker_commission_per_contract: float | None = None,
    maker_commission_bps: float | None = None,
) -> Blotter:
    """The blotter the fills imply.

    Ordered by `seq`, because the log is the record of what happened and the
    order it happened in is part of that. Sorting here rather than trusting
    append order costs nothing and removes a way for the two to differ.

    **One book per asset.** This kept a single `position` and `avg_price` for
    every fill in the log, which was exactly right while a run could only hold
    one instrument and silently wrong the moment it could hold two: a long
    call's +1 would "close" against a long put's -1 and book a realised number
    from the difference between two unrelated premiums. This function is what
    the SCORER reads -- `Engine.reconstructed()` is the blotter the gate is
    computed from, precisely so that no accumulated state can be trusted -- so
    a multi-asset run would have been scored on fiction. Found by asserting
    that the reconstruction agrees with the accumulator on a four-leg box.

    `final_position` and `final_price` stay the DEFAULT asset's, because that
    is what they have always meant and what every consumer of them assumes; a
    run's per-asset finals are available from the rows. For a single-asset run
    every number here is unchanged to the bit.
    """
    blotter = Blotter()
    books: dict[str, list] = {}          # asset -> [position, avg_price]
    last_price: dict[str, float] = {}

    for fill in sorted(fills, key=lambda f: (f.seq, f.order_id)):
        book = books.setdefault(fill.asset, [0, 0.0])
        position, avg_price = book
        signed = fill.size * fill.side.sign
        realized = 0.0
        last_price[fill.asset] = fill.price

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

        book[0], book[1] = position, avg_price
        blotter.add(
            BlotterRow(
                seq=fill.seq,
                ts=fill.ts,
                order_id=fill.order_id,
                side=fill.side,
                price=fill.price,
                size=fill.size,
                position_after=position,
                commission=fill_commission(
                    fill.price, fill.size,
                    commission_per_contract=commission_per_contract,
                    commission_bps=commission_bps,
                    slippage_per_contract=slippage_per_contract,
                    slippage_bps=slippage_bps, liquidity=fill.liquidity,
                    maker_commission_per_contract=maker_commission_per_contract,
                    maker_commission_bps=maker_commission_bps),
                realized_pnl=realized,
                tag=fill.tag,
                asset=fill.asset,
            )
        )

    blotter.final_position = books.get(DEFAULT_ASSET, [0, 0.0])[0]
    blotter.final_price = last_price.get(DEFAULT_ASSET, 0.0)
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
