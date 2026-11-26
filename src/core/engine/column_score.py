"""Score a native run from its fill columns, for runs too large to hold one object per fill.

A market maker can make tens of millions of fills over a split. `reconstruct` and `score` build a
Python object per fill, which does not fit in memory at that size. This module computes the same
numbers from plain arrays: the same reconstruction loop in the same arithmetic order, the same
per-session sums with `math.fsum`, the same time-weighted exposure, peak position and funding, and
then the unchanged `score_returns`. Tests require it to equal `score(reconstruct(...))` exactly.

Single-asset only, which is every native run.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

import numpy as np

from ..scoring import metrics
from ..scoring.scorer import score_returns
from ..types import ScoreTuple

try:  # compiled when Numba is present; the same loops run as plain Python otherwise
    from numba import njit
except ImportError:  # pragma: no cover - the research environment has no Numba
    def njit(*args, **kwargs):
        if args and callable(args[0]):
            return args[0]
        return lambda f: f


@njit(cache=False)
def _replay(side, size, price):
    """Position after each fill and realized P&L, exactly as `reconstruct` computes them."""
    n = side.size
    position_after = np.zeros(n, np.int64)
    realized = np.zeros(n, np.float64)
    avg_after = np.zeros(n, np.float64)
    position = 0
    avg_price = 0.0
    for i in range(n):
        signed = size[i] * side[i]
        p = price[i]
        if position != 0 and (position > 0) != (signed > 0):
            closing = min(abs(signed), abs(position))
            realized[i] = (p - avg_price) * closing * (1 if position > 0 else -1)
            remaining_after = position + signed
            if not ((position > 0) == (remaining_after > 0) or remaining_after == 0):
                avg_price = p
            position = remaining_after
        else:
            total = abs(position) + size[i]
            avg_price = (avg_price * abs(position) + p * size[i]) / total if total else p
            position += signed
        position_after[i] = position
        avg_after[i] = avg_price
    return position_after, realized, avg_after


@njit(cache=False)
def _held_spans(ts, position_after, end_ts):
    """`[opened, flat)` intervals, as `metrics._held_intervals` builds them for one asset."""
    n = ts.size
    lo = np.empty(n + 1, np.int64)
    hi = np.empty(n + 1, np.int64)
    k = 0
    opened = -1
    is_open = False
    for i in range(n):
        if not is_open and position_after[i] != 0:
            opened = ts[i]
            is_open = True
        elif is_open and position_after[i] == 0:
            lo[k] = opened
            hi[k] = ts[i]
            k += 1
            is_open = False
    if is_open:
        lo[k] = opened
        hi[k] = max(opened, end_ts)
        k += 1
    return lo[:k], hi[:k]


@njit(cache=False)
def _merge(lo, hi):
    """`metrics._merge_spans`: sorted, then overlapping intervals joined."""
    order = np.argsort(lo, kind="mergesort")
    out_lo = np.empty(lo.size, np.int64)
    out_hi = np.empty(lo.size, np.int64)
    k = 0
    for j in range(order.size):
        a = lo[order[j]]
        b = hi[order[j]]
        if k > 0 and a <= out_hi[k - 1]:
            if b > out_hi[k - 1]:
                out_hi[k - 1] = b
        else:
            out_lo[k] = a
            out_hi[k] = b
            k += 1
    return out_lo[:k], out_hi[:k]


@njit(cache=False)
def _exposure(lo, hi, starts, ends):
    """The seconds held per session, in the order `metrics.session_exposure` adds them."""
    out = np.zeros(starts.size, np.float64)
    for j in range(lo.size):
        a0 = lo[j]
        b0 = hi[j]
        if b0 <= a0:
            continue
        i = np.searchsorted(starts, a0, side="right") - 1
        if i < 0:
            i = 0
        while i < starts.size and starts[i] <= b0:
            a = max(a0, starts[i])
            b = min(b0, ends[i])
            if b > a:
                out[i] += float(b - a)
            i += 1
    return out


def blotter_columns(columns: dict, *, commission_per_contract: float, commission_bps: float = 0.0,
                    slippage_per_contract: float = 0.0, slippage_bps: float = 0.0,
                    maker_commission_per_contract: float | None = None,
                    maker_commission_bps: float | None = None) -> dict:
    """What `reconstruct` would put in each row, as arrays, in its order (sequence, then order id)."""
    order = _sequence_order(columns)
    pick = (lambda a: a) if order is None else (lambda a: a[order])
    side = pick(columns["side"]).astype(np.int64)
    size = pick(columns["size"]).astype(np.int64, copy=False)
    price = pick(columns["price"]).astype(np.float64, copy=False)
    maker = pick(columns["maker"]).astype(bool)
    position_after, realized, avg_after = _replay(side, size, price)
    # `fill_commission`, elementwise and in its order of operations.
    per = np.where(maker, commission_per_contract if maker_commission_per_contract is None
                   else maker_commission_per_contract, commission_per_contract)
    bps = np.where(maker, commission_bps if maker_commission_bps is None else maker_commission_bps,
                   commission_bps)
    commission = per * size + bps * 1e-4 * price * size
    if slippage_per_contract or slippage_bps:
        taker = ~maker
        commission = np.where(taker, commission + (slippage_per_contract * size
                                                   + slippage_bps * 1e-4 * price * size), commission)
    return {"seq": pick(columns["seq"]), "ts": pick(columns["ts"]), "position_after": position_after,
            "realized": realized, "commission": commission, "avg_after": avg_after}


def inventory_marks(b: dict, calendar: Sequence[metrics.SessionSpan], closes: Mapping[int, float]) -> list[float]:
    """Amendment 24 D4's inventory marks: per session, the change in the open position's value at
    that session's close, in price points.

    The value at a close is `position * (close - average entry)` after the last fill at or before the
    session's end, and zero when flat. Summed over the period, the marks plus realized P&L equal the
    realized P&L plus the final open position's value, so a position held to the end is charged its
    loss or credited its gain instead of being ignored.
    """
    by_time = np.lexsort((b["seq"], b["ts"]))
    ts = b["ts"][by_time]
    positions = b["position_after"][by_time]
    averages = b["avg_after"][by_time]
    out, previous = [], 0.0
    for span in calendar:
        k = int(np.searchsorted(ts, span.end_ts, side="right")) - 1
        q = int(positions[k]) if k >= 0 else 0
        if q == 0:
            value = 0.0
        else:
            close = closes.get(span.session)
            if close is None or not math.isfinite(close) or close <= 0:
                raise ValueError(f"no close price to mark session {span.session}, where {q} lots are open")
            value = q * (close - float(averages[k]))
        out.append(value - previous)
        previous = value
    return out


def columns_from_records(fills) -> dict:
    """Fill records as the columns this module reads, for the Python engine's own runs."""
    from ..types import Side

    return {"seq": np.array([f.seq for f in fills], np.int64), "ts": np.array([f.ts for f in fills], np.int64),
            "session": np.array([f.session for f in fills], np.int64),
            "order_id": np.array([f.order_id for f in fills], np.int64),
            "side": np.array([1 if f.side is Side.BUY else -1 for f in fills], np.int8),
            "size": np.array([f.size for f in fills], np.int64),
            "price": np.array([f.price for f in fills], np.float64),
            "maker": np.array([1 if f.liquidity == "maker" else 0 for f in fills], np.int8),
            "mid": np.array([f.mid if f.mid is not None else np.nan for f in fills], np.float64)}


def _sequence_order(columns: dict):
    """`reconstruct`'s order (sequence, then order id), or None when the columns already are in it,
    which the native engine's are, so a large run is not copied to be sorted."""
    seq, oid = columns["seq"], columns["order_id"]
    if seq.size < 2:
        return None
    step = np.diff(seq)
    if (step > 0).all() or ((step >= 0).all() and (np.diff(oid)[step == 0] > 0).all()):
        return None
    return np.lexsort((oid, seq))


def _fsum(values: np.ndarray) -> float:
    """`math.fsum`, exactly rounded, without building a Python list of every element."""
    return math.fsum(values) if values.size else 0.0


def _session_index(ts: np.ndarray, starts: np.ndarray, ends: np.ndarray) -> np.ndarray:
    """`metrics._session_of` for many timestamps: the span holding `ts`, else the one before it."""
    return np.searchsorted(starts, ts, side="right") - 1


def session_returns_columns(b: dict, calendar: Sequence[metrics.SessionSpan], *,
                            notional_base: metrics.PerSession = 1.0,
                            idle_charge: metrics.PerSession = metrics.IDLE_CHARGE,
                            funding: Sequence[metrics.FundingEvent] | None = None) -> tuple[list[float], list[float]]:
    """(`metrics.session_returns`, `metrics.session_exposure`) from `blotter_columns`."""
    metrics._check_positive(notional_base, calendar)
    starts = np.array([s.start_ts for s in calendar], np.int64)
    ends = np.array([s.end_ts for s in calendar], np.int64)
    which = _session_index(b["ts"], starts, ends)
    net = b["realized"] - b["commission"]
    sums = [0.0] * len(calendar)
    valid = which >= 0
    if valid.any():
        order = np.argsort(which[valid], kind="mergesort")
        grouped = which[valid][order]
        values = net[valid][order]
        bounds = np.flatnonzero(np.diff(grouped)) + 1
        for chunk_sessions, chunk in zip(np.split(grouped, bounds), np.split(values, bounds)):
            sums[int(chunk_sessions[0])] = _fsum(chunk)

    # Funding on the position held just before each settlement (`metrics.funding_points`).
    paid = [0.0] * len(calendar)
    if funding:
        index = {s.session: i for i, s in enumerate(calendar)}
        ts_, seq_ = b["ts"], b["seq"]
        ordered = ts_.size < 2 or bool((np.diff(ts_) > 0).all() or (
            (np.diff(ts_) >= 0).all() and (np.diff(seq_)[np.diff(ts_) == 0] >= 0).all()))
        by_time = None if ordered else np.lexsort((seq_, ts_))
        ts_sorted = ts_ if by_time is None else ts_[by_time]
        positions = b["position_after"] if by_time is None else b["position_after"][by_time]
        lo, hi = (calendar[0].start_ts, calendar[-1].end_ts) if calendar else (0, -1)
        for e in funding:
            if e.ts < lo or e.ts > hi:
                continue
            k = int(np.searchsorted(ts_sorted, e.ts, side="left")) - 1     # bisect_left
            q = int(positions[k]) if k >= 0 else 0
            if q == 0:
                continue
            s = metrics._session_of(e.ts, calendar)
            if s is None or s not in index:
                continue
            paid[index[s]] += q * e.mark * e.rate

    exposure = [0.0] * len(calendar)
    if calendar:
        spans = sorted(range(len(calendar)), key=lambda i: calendar[i].start_ts)
        s_starts = np.array([calendar[i].start_ts for i in spans], np.int64)
        s_ends = np.array([calendar[i].end_ts for i in spans], np.int64)
        lo_, hi_ = _held_spans(b["ts"], b["position_after"], int(s_ends[-1]))
        lo_, hi_ = _merge(lo_, hi_)
        held = _exposure(lo_, hi_, s_starts, s_ends)
        sorted_exposure = []
        for i in range(len(spans)):
            total = float(s_ends[i] - s_starts[i])
            sorted_exposure.append(min(1.0, held[i] / total) if total > 0 else 0.0)
        position_of = {calendar[spans[i]].session: sorted_exposure[i] for i in range(len(spans))}
        exposure = [position_of[s.session] for s in calendar]

    if not isinstance(idle_charge, Mapping) and idle_charge <= 0:
        if not funding:
            charges = [0.0] * len(calendar)
        else:
            charges = [-p / metrics.per_session(notional_base, s.session) for s, p in zip(calendar, paid)]
    else:
        peak = int(np.abs(b["position_after"]).max()) if b["position_after"].size else 0
        scale = float(peak) if peak > 0 else 1.0
        charges = [(-metrics.per_session(idle_charge, s.session) * scale * (1.0 - f) - p)
                   / metrics.per_session(notional_base, s.session)
                   for s, f, p in zip(calendar, exposure, paid)]
    returns = [sums[i] / metrics.per_session(notional_base, s.session) + c
               for i, (s, c) in enumerate(zip(calendar, charges))]
    return returns, exposure


def score_columns(columns: dict, *, costs: dict, period: Sequence[metrics.SessionSpan],
                  notional_base: metrics.PerSession = 1.0, idle_charge: metrics.PerSession = metrics.IDLE_CHARGE,
                  funding: Sequence[metrics.FundingEvent] | None = None,
                  min_exposed_fraction: float | None = None,
                  closes: Mapping[int, float] | None = None) -> tuple[ScoreTuple, list[float]]:
    """`score(reconstruct(...), ...)` and its session returns, from fill columns.

    With `closes` (session -> close price), each session's return also carries its inventory mark
    (A24 D4), and net P&L includes the final open position's value. Without them it is exactly
    `score(reconstruct(...))`.
    """
    from dataclasses import replace

    from ..scoring.scorer import MIN_DOWNSIDE_SESSIONS, MIN_SESSIONS, REJECT

    b = blotter_columns(columns, **costs)
    returns, exposure = session_returns_columns(b, period, notional_base=notional_base,
                                                idle_charge=idle_charge, funding=funding)
    net = _fsum(b["realized"]) - _fsum(b["commission"])
    if closes is not None:
        marks = inventory_marks(b, period, closes)
        returns = [r + m / metrics.per_session(notional_base, s.session)
                   for r, m, s in zip(returns, marks, period)]
        net = net + math.fsum(marks)
    trades = int(np.count_nonzero(b["realized"] != 0.0))
    exposed = -1
    if min_exposed_fraction is not None:
        exposed = sum(1 for e in exposure if e > 0)
        if exposed < min_exposed_fraction * len(period):
            return ScoreTuple(
                sessions_exposed=exposed, min_exposed_fraction=min_exposed_fraction, primary=REJECT,
                sharpe=0.0, skew=0.0, kurtosis=0.0, n_sessions=len(period), net_pnl=net, trade_count=trades,
                rejected=True,
                reject_reason=(f"exposed in {exposed} of {len(period)} scored sessions, under the registered "
                               f"floor of {min_exposed_fraction:.0%} (Amendment 6)")), returns
    out = score_returns(returns, net_pnl=net, trade_count=trades, min_sessions=MIN_SESSIONS,
                        min_downside_sessions=MIN_DOWNSIDE_SESSIONS)
    if min_exposed_fraction is None:
        return out, returns
    return replace(out, sessions_exposed=exposed, min_exposed_fraction=min_exposed_fraction), returns


def columns_digest(columns: dict) -> str:
    """A digest over the fill columns' exact bytes, in sequence order, for runs too large to hash
    one JSON row per fill. Not comparable with `log_digest`."""
    import hashlib

    order = _sequence_order(columns)
    h = hashlib.sha256()
    for name in ("seq", "ts", "session", "order_id", "side", "size", "price", "maker", "mid"):
        h.update(name.encode())
        values = columns[name] if order is None else columns[name][order]
        h.update(memoryview(np.ascontiguousarray(values)).cast("B"))
    return h.hexdigest()
