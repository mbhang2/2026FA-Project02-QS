"""Bars: synthetic random-walk by default, or vendor files under NVDA_EQUITY."""

from __future__ import annotations

import csv
import math
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from daksh.audit_agent.config import REPO_ROOT
NY = ZoneInfo("America/New_York")
VENDOR_CSV_NAME = "nvda-ohlcv-1m.csv"
_BAR_CACHE: dict[tuple[str, str, str, str, float], list[Bar]] = {}


@dataclass(frozen=True)
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    ts: datetime | None = None


def parse_iso(value: str) -> date:
    year, month, day = value.split("-")
    return date(int(year), int(month), int(day))


def experiment_data_spec(exp: dict[str, Any]) -> dict[str, Any]:
    spec = exp.get("data")
    if not isinstance(spec, dict):
        spec = {}
    source = spec.get("source") or "synthetic"
    frequency = spec.get("frequency") or ("1m" if source == "vendor" else "1d")
    root = spec.get("root") or spec.get("path") or "NVDA_EQUITY"
    return {
        "source": source,
        "frequency": frequency,
        "root": root,
        "file": spec.get("file"),
    }


def is_synthetic(exp: dict[str, Any]) -> bool:
    return experiment_data_spec(exp)["source"] == "synthetic"


def bars_for(exp: dict[str, Any], symbol: str, *, seed: int = 7) -> list[Bar]:
    spec = experiment_data_spec(exp)
    if spec["source"] == "synthetic":
        return generate_bars(symbol, exp["start"], exp["end"], seed=seed)
    if spec["source"] == "vendor":
        bars = load_vendor_bars(
            symbol,
            exp["start"],
            exp["end"],
            root=spec["root"],
            file=spec["file"],
        )
        if spec["frequency"] == "1d":
            bars = resample_daily(bars)
        return bars
    raise ValueError(f"unknown data.source: {spec['source']}")


def periods_per_year(bars: list[Bar], *, frequency: str) -> int:
    if frequency == "1d" or not bars:
        return 252
    first = bars[0].ts or datetime.combine(bars[0].day, datetime.min.time(), tzinfo=timezone.utc)
    last = bars[-1].ts or datetime.combine(bars[-1].day, datetime.min.time(), tzinfo=timezone.utc)
    span_days = max((last - first).total_seconds() / 86400.0, 1.0 / 365.25)
    years = span_days / 365.25
    return max(1, int(round(len(bars) / years)))


def generate_bars(
    symbol: str,
    start: str,
    end: str,
    *,
    seed: int = 7,
    mu: float = 0.0001,
    sigma: float = 0.012,
    start_price: float = 100.0,
) -> list[Bar]:
    """Gaussian random-walk closes; OHLC derived from the same shock.

    `symbol` is part of the seed so a universe is stable and distinct per name.
    """
    rng = random.Random(_seed_for(symbol, seed))
    start_d = parse_iso(start)
    end_d = parse_iso(end)
    if end_d < start_d:
        raise ValueError("end precedes start")
    bars: list[Bar] = []
    price = start_price
    day = start_d
    while day <= end_d:
        if day.weekday() < 5:
            shock = rng.gauss(mu, sigma)
            prev = price
            price = max(0.01, prev * math.exp(shock))
            high = max(prev, price) * (1 + abs(rng.gauss(0, 0.002)))
            low = min(prev, price) * (1 - abs(rng.gauss(0, 0.002)))
            o = prev
            volume = 1_000_000 * (1 + abs(rng.gauss(0, 0.1)))
            bars.append(
                Bar(
                    day=day,
                    open=o,
                    high=max(high, o, price),
                    low=min(low, o, price),
                    close=price,
                    volume=volume,
                    ts=datetime(day.year, day.month, day.day, tzinfo=timezone.utc),
                )
            )
        day += timedelta(days=1)
    if len(bars) < 3:
        raise ValueError("not enough weekday bars in range")
    return bars


def load_vendor_bars(
    symbol: str,
    start: str,
    end: str,
    *,
    root: str | Path = "NVDA_EQUITY",
    file: str | None = None,
) -> list[Bar]:
    start_d = parse_iso(start)
    end_d = parse_iso(end)
    csv_path = _resolve_vendor_csv(root, file)
    wanted = symbol.upper()
    cache_key = (str(csv_path.resolve()), wanted, start, end, csv_path.stat().st_mtime)
    cached = _BAR_CACHE.get(cache_key)
    if cached is not None:
        return cached
    bars: list[Bar] = []
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            row_symbol = (row.get("symbol") or wanted).upper()
            if row_symbol != wanted:
                continue
            ts = _parse_ts(row["ts"])
            day = ts.astimezone(NY).date()
            if day < start_d or day > end_d:
                continue
            bars.append(
                Bar(
                    day=day,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                    ts=ts,
                )
            )
    if len(bars) < 3:
        raise ValueError(f"not enough vendor bars for {symbol} in {start}..{end} ({csv_path})")
    _BAR_CACHE[cache_key] = bars
    return bars


def resample_daily(bars: list[Bar]) -> list[Bar]:
    """Session daily OHLCV from 1-minute bars, grouped by America/New_York date."""
    grouped: dict[date, list[Bar]] = {}
    for bar in bars:
        grouped.setdefault(bar.day, []).append(bar)
    out: list[Bar] = []
    for day in sorted(grouped):
        chunk = grouped[day]
        out.append(
            Bar(
                day=day,
                open=chunk[0].open,
                high=max(b.high for b in chunk),
                low=min(b.low for b in chunk),
                close=chunk[-1].close,
                volume=sum(b.volume for b in chunk),
                ts=chunk[-1].ts or datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc),
            )
        )
    if len(out) < 3:
        raise ValueError("not enough daily bars after resample")
    return out


def closes(bars: list[Bar]) -> list[float]:
    return [b.close for b in bars]


def simple_returns(prices: list[float]) -> list[float]:
    out = [0.0]
    for i in range(1, len(prices)):
        prev = prices[i - 1]
        out.append(0.0 if prev == 0 else prices[i] / prev - 1.0)
    return out


def _seed_for(symbol: str, seed: int) -> int:
    h = 0
    for ch in symbol:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return (seed ^ h) & 0xFFFFFFFF


def _resolve_root(root: str | Path) -> Path:
    path = Path(root)
    if path.is_absolute():
        return path
    return REPO_ROOT / path


def _resolve_vendor_csv(root: str | Path, file: str | None) -> Path:
    folder = _resolve_root(root)
    if file:
        path = Path(file)
        if not path.is_absolute():
            path = folder / path if (folder / path).exists() else REPO_ROOT / path
        if not path.is_file():
            raise FileNotFoundError(f"vendor file not found: {path}")
        if path.suffix.lower() == ".csv":
            return path
        return _materialize_csv(folder, dbn_path=path)
    csv_path = folder / VENDOR_CSV_NAME
    if csv_path.is_file() and csv_path.stat().st_size > 0:
        return csv_path
    return _materialize_csv(folder)


def _pick_year_dbn(folder: Path) -> Path:
    files = sorted(folder.glob("*.ohlcv-1m.dbn.zst")) + sorted(folder.glob("*.ohlcv-1m.dbn"))
    if not files:
        raise FileNotFoundError(
            f"no vendor OHLCV in {folder}: need {VENDOR_CSV_NAME} or a *.ohlcv-1m.dbn[.zst]"
        )
    for path in files:
        if "20240808-20250807" in path.name:
            return path
    return files[0]


def _materialize_csv(folder: Path, *, dbn_path: Path | None = None) -> Path:
    dbn_path = dbn_path or _pick_year_dbn(folder)
    dest = folder / VENDOR_CSV_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        import databento as db
    except ImportError as exc:
        raise ImportError(
            "vendor DBN decode needs the databento package, or a prebuilt "
            f"{VENDOR_CSV_NAME} in {folder}"
        ) from exc
    store = db.DBNStore.from_file(dbn_path)
    frame = store.to_df().reset_index()
    ts_col = "ts_event" if "ts_event" in frame.columns else frame.columns[0]
    symbols = frame["symbol"] if "symbol" in frame.columns else ["NVDA"] * len(frame)
    with dest.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ts", "open", "high", "low", "close", "volume", "symbol"])
        for ts, o, high, low, close, volume, symbol in zip(
            frame[ts_col],
            frame["open"],
            frame["high"],
            frame["low"],
            frame["close"],
            frame["volume"],
            symbols,
        ):
            if getattr(ts, "tzinfo", None) is None:
                parsed = ts.replace(tzinfo=timezone.utc)
            elif hasattr(ts, "tz_convert"):
                parsed = ts.tz_convert("UTC")
            else:
                parsed = ts.astimezone(timezone.utc)
            writer.writerow(
                [parsed.isoformat(), o, high, low, close, int(volume), symbol]
            )
    return dest


def _parse_ts(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    ts = datetime.fromisoformat(text)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)
