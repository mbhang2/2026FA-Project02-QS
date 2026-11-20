"""Read raw crypto-perpetual archive files from Binance, Bybit and OKX into one schema.

The three exchanges publish the same facts with different conventions, and every
difference here is one that silently corrupts a result if it is got wrong:

| | Binance (`data.binance.vision`) | Bybit (`public.bybit.com`) | OKX (`static.okx.com`) |
|---|---|---|---|
| aggressor | `is_buyer_maker=true` means the **seller** hit the bid | `side` is the taker's side | `side` is the taker's side |
| timestamp | ms UTC | **seconds** UTC as a decimal string | ms UTC |
| size unit | base coin | base coin (linear USDT perps) | **contracts**; times `ctVal` for base coin |
| daily file covers | UTC day | UTC day; **rows descending** in older files | **UTC+8 day** (16:00 UTC to 16:00 UTC) |

So a reader never trusts row order and never assumes a file is a UTC day: it
returns rows sorted by time and the caller re-buckets by timestamp.

Normalised trades: `ts_ns` (int64, UTC nanoseconds), `price` (float64),
`size` (float64, base coin), `side` (int8: +1 buyer was the aggressor, -1 seller),
`trade_id` (str). Every exchange here reports the aggressor for every trade, so
unlike the NQ feed there is no unknown-side volume.

Nothing here touches the network; `scripts/crypto_fetch.py` writes the files
these read, and `data/crypto-manifest.json` records what it wrote.
"""

from __future__ import annotations

import gzip
import io
import os
import zipfile
from datetime import date as dt_date
from pathlib import Path

import numpy as np
import pandas as pd

EXCHANGES = ("binance", "bybit", "okx")

TRADE_COLUMNS = ("ts_ns", "price", "size", "side", "trade_id")
KLINE_COLUMNS = ("ts_ns", "open", "high", "low", "close", "volume", "buy_volume")
FUNDING_COLUMNS = ("ts_ns", "rate")

_MS = 1_000_000


def _open_text(path: Path) -> io.TextIOBase:
    """A zip with one member, a gzip, or plain text: the archives use all three."""
    path = Path(path)
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            members = [n for n in zf.namelist() if not n.endswith("/")]
            if len(members) != 1:
                raise ValueError(f"{path}: expected one member, found {members}")
            return io.StringIO(zf.read(members[0]).decode("utf-8"))
    if path.suffix == ".gz":
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8")
    return open(path, encoding="utf-8")


def _read_csv(path: Path, columns: list[str]) -> pd.DataFrame:
    """Read a CSV whose header row may be missing (early Binance files have none)."""
    with _open_text(path) as fh:
        text = fh.read()
    first = text.split("\n", 1)[0].strip()
    has_header = bool(first) and not (first[0].isdigit() or first[0] == "-")
    df = pd.read_csv(io.StringIO(text), header=0 if has_header else None, dtype=str)
    if not has_header:
        df.columns = columns[: df.shape[1]]
    return df


def _seconds_str_to_ns(s: pd.Series) -> np.ndarray:
    """'1794873600.0814' -> ns, exactly: float64 seconds lose ~100 ns at this magnitude."""
    parts = s.astype(str).str.split(".", n=1, expand=True)
    whole = parts[0].astype("int64")
    frac = parts[1] if parts.shape[1] > 1 else pd.Series("", index=s.index)
    frac = frac.fillna("").str.ljust(9, "0").str.slice(0, 9).astype("int64")
    return (whole * 1_000_000_000 + frac).to_numpy(dtype="int64")


def _finish(df: pd.DataFrame, columns: tuple[str, ...]) -> pd.DataFrame:
    out = df.loc[:, list(columns)].sort_values("ts_ns", kind="stable")
    return out.reset_index(drop=True)


def read_trades(exchange: str, path: str | Path, *, contract_value: float | None = None) -> pd.DataFrame:
    """One archive file of trades in the normalised schema, sorted by time.

    `contract_value` is required for OKX (its sizes are contracts) and refused
    elsewhere, so a caller cannot forget it on the one exchange that needs it or
    apply it twice on the others.
    """
    path = Path(path)
    if exchange == "okx":
        if not contract_value or contract_value <= 0:
            raise ValueError("OKX sizes are contracts: pass contract_value (ctVal)")
    elif contract_value is not None:
        raise ValueError(f"{exchange} sizes are already in base coin; contract_value is OKX only")

    if exchange == "binance":
        df = _read_csv(path, ["agg_trade_id", "price", "quantity", "first_trade_id",
                              "last_trade_id", "transact_time", "is_buyer_maker"])
        maker = df["is_buyer_maker"].str.lower().map({"true": True, "false": False})
        if maker.isna().any():
            raise ValueError(f"{path}: unreadable is_buyer_maker values")
        out = pd.DataFrame({
            "ts_ns": df["transact_time"].astype("int64") * _MS,
            "price": df["price"].astype("float64"),
            "size": df["quantity"].astype("float64"),
            # buyer was the maker -> the seller crossed the spread
            "side": np.where(maker, -1, 1).astype("int8"),
            "trade_id": df["agg_trade_id"].astype(str),
        })
    elif exchange == "bybit":
        df = _read_csv(path, ["timestamp", "symbol", "side", "size", "price"])
        side = df["side"].str.lower().map({"buy": 1, "sell": -1})
        if side.isna().any():
            raise ValueError(f"{path}: unreadable side values")
        out = pd.DataFrame({
            "ts_ns": _seconds_str_to_ns(df["timestamp"]),
            "price": df["price"].astype("float64"),
            "size": df["size"].astype("float64"),
            "side": side.astype("int8"),
            "trade_id": df["trdMatchID"].astype(str) if "trdMatchID" in df else "",
        })
    elif exchange == "okx":
        df = _read_csv(path, ["instrument_name", "trade_id", "side", "price", "size", "created_time"])
        side = df["side"].str.lower().map({"buy": 1, "sell": -1})
        if side.isna().any():
            raise ValueError(f"{path}: unreadable side values")
        out = pd.DataFrame({
            "ts_ns": df["created_time"].astype("int64") * _MS,
            "price": df["price"].astype("float64"),
            "size": df["size"].astype("float64") * float(contract_value or 0),
            "side": side.astype("int8"),
            "trade_id": df["trade_id"].astype(str),
        })
    else:
        raise ValueError(f"unknown exchange {exchange!r}; expected one of {EXCHANGES}")
    return _finish(out, TRADE_COLUMNS)


def read_klines(exchange: str, path: str | Path) -> pd.DataFrame:
    """One-minute bars, stamped with their open. `buy_volume` is NaN where the
    exchange does not publish the taker-buy split (OKX)."""
    path = Path(path)
    if exchange == "binance":
        df = _read_csv(path, ["open_time", "open", "high", "low", "close", "volume",
                              "close_time", "quote_volume", "count", "taker_buy_volume",
                              "taker_buy_quote_volume", "ignore"])
        out = pd.DataFrame({
            "ts_ns": df["open_time"].astype("int64") * _MS,
            **{c: df[c].astype("float64") for c in ("open", "high", "low", "close", "volume")},
            "buy_volume": df["taker_buy_volume"].astype("float64"),
        })
    elif exchange == "okx":
        df = _read_csv(path, ["instrument_name", "open", "high", "low", "close", "vol",
                              "vol_ccy", "vol_quote", "open_time", "confirm"])
        out = pd.DataFrame({
            "ts_ns": df["open_time"].astype("int64") * _MS,
            **{c: df[c].astype("float64") for c in ("open", "high", "low", "close")},
            "volume": df["vol_ccy"].astype("float64"),   # base coin; `vol` is contracts
            "buy_volume": np.nan,
        })
    else:
        raise ValueError(f"no kline archive is read for {exchange!r}")
    # OKX repeats whole rows inside some files (2023-10-12, 2024-06-25: identical
    # copies). Drop exact repeats; a minute that repeats with different values is
    # not a repeat, and is refused rather than resolved by guessing.
    out = out.drop_duplicates()
    if out["ts_ns"].duplicated().any():
        raise ValueError(f"{path}: the same minute appears with different values")
    return _finish(out, KLINE_COLUMNS)


def read_funding(exchange: str, path: str | Path) -> pd.DataFrame:
    """Funding settlements: the rate paid by longs to shorts (negative: the reverse)."""
    path = Path(path)
    if exchange == "binance":
        df = _read_csv(path, ["calc_time", "funding_interval_hours", "last_funding_rate"])
        out = pd.DataFrame({
            "ts_ns": df["calc_time"].astype("int64") * _MS,
            "rate": df["last_funding_rate"].astype("float64"),
        })
    elif exchange == "okx":
        df = _read_csv(path, ["instrument_name", "funding_rate", "funding_time"])
        out = pd.DataFrame({
            "ts_ns": df["funding_time"].astype("int64") * _MS,
            "rate": df["funding_rate"].astype("float64"),
        })
    else:
        raise ValueError(f"no funding archive is read for {exchange!r}")
    return _finish(out, FUNDING_COLUMNS)


# --------------------------------------------------------------------------- #
# OKX order-book quotes (market-data-history module 6, "tick-by-tick")
# --------------------------------------------------------------------------- #
# One CSV per UTC day (by exchange time; unlike OKX's trade files, which are
# UTC+8 days): `timeMs` (OKX's receive/publish time), `exchTimeMs` (the matching
# engine's time) and the best 50 bid and ask levels as `bid_<i>_px`,
# `bid_<i>_qty`, `bid_<i>_ordCnt`, `ask_<i>_...`, one row per book change.
# Sizes are CONTRACTS, like OKX trades. A BTC day is ~7M rows and ~450 MB gzipped.
#
# Compacted on download to the top `levels` levels, keeping only the rows where
# one of them changed: lossless for that depth, ~60% of rows and ~13% of bytes.

QUOTE_LEVELS = 5
QUOTE_FIELDS = ("px", "qty", "ordCnt")
# Some OKX days carry a corrupt stretch: on 2026-11-02 at 10:04:18.842 UTC the
# clock steps back 9,950 ms and 812 rows are stamped 10:04:08.892 .. 18.822;
# 421 of them are CROSSED (best bid above best ask by up to $5.40), and after
# them the book resumes exactly as it was before. Those rows are not book states
# and are DROPPED: a row stamped earlier than any row before it (exchange time
# below the running maximum) is removed, counted and recorded. A step back of
# more than a minute is refused as something other than this.
MAX_BACKSTEP_MS = 60_000


def quote_columns(levels: int = QUOTE_LEVELS) -> list[str]:
    """The book columns kept, in storage order: level-major, bid then ask, px/qty/count."""
    return [f"{side}_{i}_{f}" for i in range(1, levels + 1)
            for side in ("bid", "ask") for f in QUOTE_FIELDS]


# OKX changed the file's column names between January 2024 and January 2025.
# Older files name levels `bidPx1` .. `askCnt50` in no particular order and add a
# `maxLevel` column; newer ones use `bid_1_px` .. `ask_50_ordCnt`. Same data, same
# units (sizes in contracts). Mapped onto the new names here, by header.
_OLD_QUOTE_FIELDS = {"px": "Px", "qty": "Sz", "ordCnt": "Cnt"}


def _okx_quote_header(src: str | Path) -> list[str]:
    with gzip.open(src, "rt") as fh:
        line = fh.readline()
    return [c.strip().strip('"') for c in line.rstrip("\r\n").split(",")]


def _okx_quote_source_columns(header: list[str], levels: int) -> dict[str, str]:
    """{source column: canonical column} for the time columns and the kept book."""
    have = set(header)
    canonical = quote_columns(levels)
    if "bid_1_px" in have:
        mapping = {c: c for c in canonical}
    elif "bidPx1" in have:
        mapping = {}
        for c in canonical:
            side, i, field = c.split("_")
            mapping[f"{side}{_OLD_QUOTE_FIELDS[field]}{i}"] = c
    else:
        raise ValueError(f"unrecognised OKX quotes header, starting {header[:6]}")
    mapping.update({"timeMs": "timeMs", "exchTimeMs": "exchTimeMs"})
    missing = [s for s in mapping if s not in have]
    if missing:
        raise ValueError(f"OKX quotes header lacks {missing[:6]}")
    return mapping


def compact_okx_quotes(src: str | Path, dest: str | Path, *, day: "dt_date",
                       levels: int = QUOTE_LEVELS, chunksize: int = 1_000_000) -> dict:
    """Read one OKX tick-by-tick file to the end and write the compact `.npz`.

    Raises -- and writes nothing -- if the file is not what it should be: no rows,
    exchange time stepping back by more than `MAX_BACKSTEP_MS`, or a row stamped
    outside `day` (one second of slack either side: the first row of a day is
    routinely a few ms before midnight). Returns the facts the manifest records.

    **Rows stamped back in time are dropped, not re-sorted.** Exchange time is
    almost monotone -- 2026-11-16 had one 20 ms step back in 7.1M rows -- but 23
    days of 2023-2026 carry a corrupt ~10 s stretch (see `MAX_BACKSTEP_MS`) whose
    rows are crossed and are followed by the book as it was before them. A row
    whose exchange time is below the running maximum is removed before anything
    else; `backsteps`, `max_backstep_ms` and `dropped_backdated` record it, and
    `crossed` counts what is left. The kept rows are in publish order with a
    non-decreasing clock.
    """
    import datetime as _dt

    book_cols = quote_columns(levels)
    cols = ["timeMs", "exchTimeMs", *book_cols]
    rename = _okx_quote_source_columns(_okx_quote_header(src), levels)
    lo = int(_dt.datetime(day.year, day.month, day.day, tzinfo=_dt.timezone.utc).timestamp() * 1000)
    lo_ok, hi_ok = lo - 1_000, lo + 86_400_000 + 1_000

    recv, exch, book = [], [], []
    prev_row: np.ndarray | None = None
    prev_exch = -1
    rows = crossed = backsteps = max_back = backdated = 0
    running_max = -1
    for ch in pd.read_csv(src, usecols=list(rename), chunksize=chunksize, dtype="float64"):
        ch = ch.rename(columns=rename)[cols]
        e = ch["exchTimeMs"].to_numpy(np.int64)
        if len(e) == 0:
            continue
        steps = np.diff(np.concatenate(([prev_exch], e)) if prev_exch >= 0 else e)
        back = -steps[steps < 0]
        if len(back):
            backsteps += len(back)
            max_back = max(max_back, int(back.max()))
            if max_back > MAX_BACKSTEP_MS:
                raise ValueError(f"{src}: exchange time steps back {max_back} ms")
        if e.min() < lo_ok or e.max() > hi_ok:
            raise ValueError(f"{src}: rows stamped outside {day} UTC")
        prev_exch = int(e[-1])
        cummax = np.maximum.accumulate(np.concatenate(([running_max], e)))[1:]
        on_time = e >= cummax
        rows += len(on_time)                       # every row read, dropped or kept
        backdated += int((~on_time).sum())
        running_max = int(cummax[-1])
        ch, e = ch[on_time], e[on_time]
        if len(e) == 0:
            continue
        b = ch[book_cols].to_numpy(np.float64)
        keep = np.ones(len(b), dtype=bool)
        keep[1:] = np.any(b[1:] != b[:-1], axis=1)
        if prev_row is not None:
            keep[0] = bool(np.any(b[0] != prev_row))
        prev_row = b[-1].copy()
        crossed += int((b[:, 0] >= b[:, 3]).sum())        # bid_1_px >= ask_1_px
        recv.append(ch["timeMs"].to_numpy(np.int64)[keep])
        exch.append(e[keep])
        book.append(b[keep])
    if rows == 0:
        raise ValueError(f"{src}: no rows")

    out = {"recv_ms": np.concatenate(recv), "exch_ms": np.concatenate(exch),
           "book": np.concatenate(book), "columns": np.array(book_cols)}
    dest = Path(dest)
    tmp = dest.with_name(dest.name + ".tmp")
    with open(tmp, "wb") as fh:
        np.savez_compressed(fh, **out)
    os.replace(tmp, dest)
    return {"rows": rows, "kept": int(len(out["exch_ms"])), "crossed": crossed,
            "levels": levels, "backsteps": backsteps, "max_backstep_ms": max_back,
            "dropped_backdated": backdated}


def read_quotes(path: str | Path, *, contract_value: float) -> dict:
    """A compact OKX quotes file as arrays: `ts_ns` (exchange time, non-decreasing),
    `recv_ns`, and
    `bid_px`, `bid_qty`, `bid_cnt`, `ask_px`, `ask_qty`, `ask_cnt`, each
    (rows, levels) with level 1 the best. Quantities in base coin
    (contracts x `contract_value`)."""
    if not contract_value or contract_value <= 0:
        raise ValueError("OKX sizes are contracts: pass contract_value (ctVal)")
    with np.load(path, allow_pickle=False) as z:
        book, cols = z["book"], [str(c) for c in z["columns"]]
        exch = z["exch_ms"].astype(np.int64)
        recv = z["recv_ms"].astype(np.int64)
    # Backdated rows are dropped here too, by the same rule as compaction. Files
    # compacted before 2026-11-20 kept steps back of under a second (a few rows a
    # day); this makes every file read the same way.
    on_time = exch >= np.maximum.accumulate(exch)
    book, exch, recv = book[on_time], exch[on_time], recv[on_time]
    out = {"ts_ns": exch * _MS, "recv_ns": recv * _MS}
    levels = len(cols) // 6
    grid = book.reshape(len(book), levels, 2, 3)               # rows, level, side, field
    for s, side in enumerate(("bid", "ask")):
        out[f"{side}_px"] = grid[:, :, s, 0]
        out[f"{side}_qty"] = grid[:, :, s, 1] * contract_value
        out[f"{side}_cnt"] = grid[:, :, s, 2].astype(np.int64)
    return out
