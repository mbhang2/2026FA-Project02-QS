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
import zipfile
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
