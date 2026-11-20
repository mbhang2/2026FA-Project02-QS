"""Crypto archives: each exchange's conventions normalise to one schema, and the fetcher's listing parsers.

No network. The conventions pinned here are the ones that corrupt a result
silently when wrong: Binance's inverted aggressor flag, Bybit's seconds and
descending rows, OKX's contract-denominated sizes.
"""

from __future__ import annotations

import gzip
import importlib.util
import pathlib
import zipfile

import sys

import pytest

from src.core.data.crypto_raw import read_funding, read_klines, read_trades

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("crypto_fetch", REPO_ROOT / "scripts" / "crypto_fetch.py")
cf = importlib.util.module_from_spec(_spec)
sys.modules["crypto_fetch"] = cf          # its dataclasses look their module up by name
_spec.loader.exec_module(cf)


def _zip(path: pathlib.Path, text: str) -> pathlib.Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(path.stem + ".csv", text)
    return path


def test_binance_buyer_maker_means_the_seller_was_the_aggressor(tmp_path):
    p = _zip(tmp_path / "BTCUSDT-aggTrades-2026-11-17.zip",
             "agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker\n"
             "2,100.5,0.2,11,11,1794873600200,true\n"
             "1,100.0,0.1,10,10,1794873600100,false\n")
    df = read_trades("binance", p)
    assert df["ts_ns"].tolist() == [1794873600100_000_000, 1794873600200_000_000]
    assert df["side"].tolist() == [1, -1]
    assert df["size"].tolist() == [0.1, 0.2]


def test_binance_files_without_a_header_row_still_read(tmp_path):
    p = _zip(tmp_path / "BTCUSDT-aggTrades-2020-01-01.zip",
             "1,7200.0,0.5,10,10,1577836800000,False\n")
    df = read_trades("binance", p)
    assert df["side"].tolist() == [1] and df["price"].tolist() == [7200.0]


def test_bybit_seconds_are_exact_and_descending_rows_are_sorted(tmp_path):
    p = tmp_path / "BTCUSDT2021-01-01.csv.gz"
    with gzip.open(p, "wt") as fh:
        fh.write("timestamp,symbol,side,size,price,tickDirection,trdMatchID,grossValue,homeNotional,foreignNotional\n"
                 "1609545599.7328,BTCUSDT,Sell,0.002,29325.0,MinusTick,b,1,0.002,58.65\n"
                 "1609459200.0814,BTCUSDT,Buy,0.001,29000.0,PlusTick,a,1,0.001,29.0\n"
                 "1609459200,BTCUSDT,Buy,0.003,29000.0,PlusTick,c,1,0.003,87.0\n")
    df = read_trades("bybit", p)
    assert df["ts_ns"].tolist() == [1609459200_000000000, 1609459200_081400000, 1609545599_732800000]
    assert df["side"].tolist() == [1, 1, -1]
    assert df["trade_id"].tolist() == ["c", "a", "b"]


def test_okx_sizes_are_contracts_and_need_the_contract_value(tmp_path):
    p = _zip(tmp_path / "BTC-USDT-SWAP-trades-2026-11-17.zip",
             "instrument_name,trade_id,side,price,size,created_time,source\n"
             "BTC-USDT-SWAP,1,sell,64434.0,6.1,1794844800356,0\n")
    with pytest.raises(ValueError, match="contract_value"):
        read_trades("okx", p)
    df = read_trades("okx", p, contract_value=0.01)
    assert df["size"].iloc[0] == pytest.approx(0.061)
    assert df["side"].iloc[0] == -1


def test_contract_value_is_refused_where_sizes_are_already_base_coin(tmp_path):
    p = _zip(tmp_path / "x.zip", "agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker\n")
    with pytest.raises(ValueError, match="OKX only"):
        read_trades("binance", p, contract_value=0.01)


def test_klines_and_funding_normalise(tmp_path):
    k = read_klines("binance", _zip(tmp_path / "k.zip",
        "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore\n"
        "1794873600000,1,2,0.5,1.5,10,1794873659999,15,3,6,9,0\n"))
    assert k.loc[0, "buy_volume"] == 6 and k.loc[0, "ts_ns"] == 1794873600000_000_000
    o = read_klines("okx", _zip(tmp_path / "c.zip",
        "instrument_name,open,high,low,close,vol,vol_ccy,vol_quote,open_time,confirm\n"
        "BTC-USDT-SWAP,1,2,0.5,1.5,3780,37.8,3179366,1790784000000,1\n"))
    assert o.loc[0, "volume"] == 37.8 and o["buy_volume"].isna().all()
    fb = read_funding("binance", _zip(tmp_path / "fb.zip",
        "calc_time,funding_interval_hours,last_funding_rate\n1790812800001,8,0.00007981\n"))
    fo = read_funding("okx", _zip(tmp_path / "fo.zip",
        "instrument_name,funding_rate,funding_time\nBTC-USDT-SWAP,0.0000367,1790784000000\n"))
    assert fb["rate"].iloc[0] == pytest.approx(7.981e-5)
    assert fo["ts_ns"].iloc[0] == 1790784000000_000_000


def test_identical_repeated_bars_are_dropped_and_conflicting_ones_refused(tmp_path):
    head = "instrument_name,open,high,low,close,vol,vol_ccy,vol_quote,open_time,confirm\n"
    row = "BTC-USDT-SWAP,1,2,0.5,1.5,3780,37.8,3179366,1790784000000,1\n"
    assert len(read_klines("okx", _zip(tmp_path / "a.zip", head + row + row))) == 1
    other = row.replace(",1.5,", ",1.6,")
    with pytest.raises(ValueError, match="different values"):
        read_klines("okx", _zip(tmp_path / "b.zip", head + row + other))


# ---- the fetcher ----------------------------------------------------------- #
S3_PAGE = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>data.binance.vision</Name>
<IsTruncated>true</IsTruncated>
<Contents><Key>data/futures/um/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2026-11-16.zip</Key><Size>123</Size></Contents>
<Contents><Key>data/futures/um/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2026-11-16.zip.CHECKSUM</Key><Size>99</Size></Contents>
</ListBucketResult>"""


def test_s3_listing_parses_keys_sizes_and_truncation():
    items, truncated = cf.parse_s3_listing(S3_PAGE)
    assert truncated
    assert items[0] == ("data/futures/um/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2026-11-16.zip", 123)
    assert len(items) == 2


def test_bybit_listing_takes_only_this_symbols_daily_files():
    html = ('<li><a href="BTCUSDT2026-11-17.csv.gz">x</a></li>'
            '<li><a href="BTCUSDTPERP2026-11-17.csv.gz">x</a></li>'
            '<li><a href="BTCUSDT2026-11-16.csv.gz">x</a></li>')
    assert cf.parse_bybit_listing(html, "BTCUSDT") == ["BTCUSDT2026-11-16.csv.gz", "BTCUSDT2026-11-17.csv.gz"]


def test_okx_listing_raises_on_an_error_code_instead_of_returning_nothing():
    with pytest.raises(RuntimeError, match="50076"):
        cf.parse_okx_listing({"code": "50076", "msg": "cannot exceed 10 days", "data": []})
    ok = {"code": "0", "data": [{"details": [{"groupDetails": [
        {"filename": "BTC-USDT-SWAP-trades-2026-11-17.zip", "url": "https://x/a.zip?v=1", "sizeMB": "17.99"}]}]}]}
    assert cf.parse_okx_listing(ok) == [("BTC-USDT-SWAP-trades-2026-11-17.zip", "https://x/a.zip?v=1", 17.99)]


def test_periods_ranges_and_gaps():
    import datetime as dt
    assert cf.period_of("BTCUSDT-fundingRate-2026-09.zip") == "2026-09"
    assert cf.period_of("BTCUSDT2026-11-17.csv.gz") == "2026-11-17"
    s, e = dt.date(2026, 9, 15), dt.date(2026, 10, 1)
    assert cf.in_range("2026-09", s, e) and cf.in_range("2026-10", s, e)
    assert not cf.in_range("2026-11", s, e) and not cf.in_range("2026-08-31", s, e)
    assert cf.daily_gaps(["2026-11-01", "2026-11-04", "2026-11-02"]) == ["2026-11-03"]
    assert cf.okx_inst_id("ETHUSDT") == "ETH-USDT-SWAP"


def test_a_file_in_r2_counts_as_held_even_after_the_local_copy_is_dropped(tmp_path):
    have = {"crypto/raw/a": [1, "s", "u", "v", "t", "bucket"], "crypto/raw/b": [1, "s", "u", "v", "t", None]}
    assert cf.held("crypto/raw/a", have, tmp_path)
    assert not cf.held("crypto/raw/b", have, tmp_path)       # recorded, but neither on disk nor in R2
    assert not cf.held("crypto/raw/c", have, tmp_path)


def test_the_lake_may_not_live_inside_the_repository(monkeypatch):
    monkeypatch.setenv("CRYPTO_LAKE", str(REPO_ROOT / "data" / "lake"))
    with pytest.raises(SystemExit, match="inside the repository"):
        cf.lake_root({"lake_root": "~/dl/crypto"})


# ---- the Dataset adapter ---------------------------------------------------- #
def _lake(tmp_path, days, *, qty="0.005"):
    """A local lake and manifest holding Binance BTCUSDT trades for `days`."""
    import datetime as dt
    import json

    files = {}
    for d in days:
        key = f"crypto/raw/binance/trades/BTCUSDT/BTCUSDT-aggTrades-{d}.zip"
        p = tmp_path / "lake" / key
        p.parent.mkdir(parents=True, exist_ok=True)
        t0 = int(dt.datetime.fromisoformat(d).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
        _zip(p, "agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker\n"
                f"1,100.0,{qty},1,1,{t0 + 5},false\n2,101.0,0.002,2,2,{t0 + 9},true\n")
        files[key] = [p.stat().st_size, "sha-" + d, "u", "v", "t", None]
    man = tmp_path / "m.json"
    man.write_text(json.dumps({"files": files}))
    return man, tmp_path / "lake"


def test_the_adapter_serves_integer_lots_with_a_known_aggressor(tmp_path):
    from src.core.data.crypto import CryptoDataset, CryptoSpec

    man, lake = _lake(tmp_path, ["2026-01-01", "2026-01-02"])
    ds = CryptoDataset(spec=CryptoSpec(cache=False), manifest_path=man, lake=lake)
    ticks = list(ds.ticks())
    assert [t.size for t in ticks] == [5, 2, 5, 2]
    assert [t.side.value for t in ticks[:2]] == ["buy", "sell"]
    lo, hi = ds.session_bounds(ticks[0].session)
    assert lo <= ticks[0].ts <= hi and hi - lo == 86_400 * 10**9 - 1


def test_a_quantity_that_is_not_a_whole_lot_is_refused(tmp_path):
    from src.core.data.crypto import CryptoDataset, CryptoSpec

    man, lake = _lake(tmp_path, ["2026-01-01"], qty="0.0005")
    ds = CryptoDataset(spec=CryptoSpec(cache=False), manifest_path=man, lake=lake)
    with pytest.raises(ValueError, match="whole number"):
        ds.load(ds.sessions()[0])


def test_a_missing_day_inside_the_range_is_refused_not_skipped(tmp_path):
    from src.core.data.crypto import CryptoDataset, CryptoSpec

    man, lake = _lake(tmp_path, ["2026-01-01", "2026-01-03"])
    with pytest.raises(ValueError, match="missing inside the range"):
        CryptoDataset(spec=CryptoSpec(cache=False), manifest_path=man, lake=lake)


def test_the_digest_pins_the_recorded_bytes(tmp_path):
    import json

    from src.core.data.crypto import CryptoDataset, CryptoSpec

    man, lake = _lake(tmp_path, ["2026-01-01"])
    a = CryptoDataset(spec=CryptoSpec(cache=False), manifest_path=man, lake=lake).digest()
    m = json.loads(man.read_text())
    next(iter(m["files"].values()))[1] = "different"
    man.write_text(json.dumps(m))
    b = CryptoDataset(spec=CryptoSpec(cache=False), manifest_path=man, lake=lake).digest()
    assert a != b


def test_the_holdout_is_locked(tmp_path):
    from src.core.data.crypto import CryptoDataset, CryptoSpec
    from src.core.types import HoldoutLocked, Split

    man, lake = _lake(tmp_path, ["2026-01-01"])
    ds = CryptoDataset(spec=CryptoSpec(cache=False), manifest_path=man, lake=lake)
    with pytest.raises(HoldoutLocked):
        next(ds.ticks(Split.HOLDOUT))


# ---- OKX order-book quotes -------------------------------------------------- #
def _tbt(path, rows):
    """A tiny OKX tick-by-tick file: (exch_ms, bid1, ask1, deep) per row, 6 levels."""
    cols = ["timeMs", "exchTimeMs"] + [f"{s}_{i}_{f}" for i in range(1, 7)
                                       for s in ("bid", "ask") for f in ("px", "qty", "ordCnt")]
    lines = [",".join(f'"{c}"' for c in cols)]
    for e, b1, a1, deep in rows:
        vals = [e + 25, e]
        for i in range(1, 7):
            px_b, px_a = (b1, a1) if i == 1 else (b1 - i, a1 + i)
            q = deep if i == 6 else 1.0          # level 6 is outside the kept top 5
            vals += [px_b, q, 1, px_a, q, 1]
        lines.append(",".join(str(v) for v in vals))
    with gzip.open(path, "wt") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def test_quotes_keep_only_rows_where_the_top_five_changed(tmp_path):
    import datetime as dt

    from src.core.data.crypto_raw import compact_okx_quotes, read_quotes

    t0 = 1794787200000                                      # 2026-11-16 00:00 UTC
    src = _tbt(tmp_path / "q.csv.gz", [(t0 + 1, 100.0, 100.1, 5.0),
                                       (t0 + 2, 100.0, 100.1, 9.0),     # only level 6 moved
                                       (t0 + 3, 100.0, 100.2, 9.0)])
    facts = compact_okx_quotes(src, tmp_path / "q.npz", day=dt.date(2026, 11, 16), chunksize=1)
    assert facts["rows"] == 3 and facts["kept"] == 2 and facts["crossed"] == 0
    q = read_quotes(tmp_path / "q.npz", contract_value=0.01)
    assert q["ts_ns"].tolist() == [(t0 + 1) * 10**6, (t0 + 3) * 10**6]
    assert q["ask_px"][:, 0].tolist() == [100.1, 100.2]
    assert q["bid_px"].shape == (2, 5) and q["bid_qty"][0, 0] == pytest.approx(0.01)


def test_quotes_refuse_a_clock_that_runs_back_or_a_row_from_another_day(tmp_path):
    import datetime as dt

    from src.core.data.crypto_raw import compact_okx_quotes

    t0 = 1794787200000
    back = _tbt(tmp_path / "b.csv.gz", [(t0 + 5_000, 1.0, 2.0, 1.0), (t0 + 1_000, 1.0, 2.5, 1.0)])
    with pytest.raises(ValueError, match="steps back"):
        compact_okx_quotes(back, tmp_path / "b.npz", day=dt.date(2026, 11, 16))
    late = _tbt(tmp_path / "l.csv.gz", [(t0 + 86_400_000 + 5_000, 1.0, 2.0, 1.0)])
    with pytest.raises(ValueError, match="outside"):
        compact_okx_quotes(late, tmp_path / "l.npz", day=dt.date(2026, 11, 16))
    assert not (tmp_path / "b.npz").exists() and not (tmp_path / "l.npz").exists()


def test_okx_quotes_take_their_day_from_the_index_not_the_filename():
    payload = {"code": "0", "data": [{"details": [{"groupDetails": [
        {"dateTs": "1794787200000", "filename": "BTC-USDT-SWAP.OK.csv.gz",
         "url": "http://x/BTC-USDT-SWAP.OK.csv.gz?Expires=1", "sizeMB": "426.73"}]}]}]}
    assert cf.okx_quotes_entries(payload) == [("2026-11-16", "http://x/BTC-USDT-SWAP.OK.csv.gz?Expires=1", 426.73)]
    f = cf.RemoteFile("okx", "quotes", "BTCUSDT", "2026-11-16", "u", None, False,
                      "BTCUSDT-quotes-top5-2026-11-16.npz")
    assert f.key == "crypto/compact/okx/quotes/BTCUSDT/BTCUSDT-quotes-top5-2026-11-16.npz"
    assert cf.period_of(f.filename) == "2026-11-16"


def test_quotes_are_fetched_only_when_asked_and_into_their_own_manifest():
    import argparse

    cfg = cf.load_config()
    bare = argparse.Namespace(cmd="fetch", exchange=None, kind=None, symbol=None)
    assert all(k != "quotes" for _, k, _ in cf.selection(cfg, bare))
    asked = argparse.Namespace(cmd="fetch", exchange=["okx"], kind=["quotes"], symbol=["BTCUSDT"])
    assert cf.manifest_for(cfg, asked) == cf.QUOTES_MANIFEST
    mixed = argparse.Namespace(cmd="fetch", exchange=["okx"], kind=["quotes", "trades"], symbol=None)
    with pytest.raises(SystemExit, match="on their own"):
        cf.manifest_for(cfg, mixed)


def test_quotes_read_okxs_older_column_names_the_same_way(tmp_path):
    """Files before 2025 name levels `bidPx1`..`askCnt50`, unordered, plus `maxLevel`."""
    import datetime as dt
    import random

    from src.core.data.crypto_raw import compact_okx_quotes, read_quotes

    t0 = 1672531200000                                      # 2023-01-01 00:00 UTC
    cols = ["exchTimeMs", "timeMs", "maxLevel"] + [f"{s}{f}{i}" for i in range(1, 7)
                                                   for s in ("bid", "ask") for f in ("Px", "Sz", "Cnt")]
    random.Random(1).shuffle(cols)
    def row(e, a1):
        v = {"exchTimeMs": e, "timeMs": e + 3, "maxLevel": 400}
        for i in range(1, 7):
            v.update({f"bidPx{i}": 100.0 - i, f"bidSz{i}": 2.0, f"bidCnt{i}": 1,
                      f"askPx{i}": a1 + i - 1, f"askSz{i}": 3.0, f"askCnt{i}": 2})
        return ",".join(str(v[c]) for c in cols)
    src = tmp_path / "old.csv.gz"
    with gzip.open(src, "wt") as fh:
        fh.write(",".join(cols) + "\n" + row(t0 + 1, 100.5) + "\n" + row(t0 + 2, 100.6) + "\n")
    facts = compact_okx_quotes(src, tmp_path / "o.npz", day=dt.date(2023, 1, 1))
    assert facts["kept"] == 2
    q = read_quotes(tmp_path / "o.npz", contract_value=0.01)
    assert q["bid_px"][0].tolist() == [99.0, 98.0, 97.0, 96.0, 95.0]
    assert q["ask_px"][:, 0].tolist() == [100.5, 100.6]
    assert q["ask_qty"][0, 0] == pytest.approx(0.03) and q["ask_cnt"][0, 0] == 2


def test_an_unknown_quotes_header_is_refused_not_guessed(tmp_path):
    import datetime as dt

    from src.core.data.crypto_raw import compact_okx_quotes

    src = tmp_path / "x.csv.gz"
    with gzip.open(src, "wt") as fh:
        fh.write("a,b,c\n1,2,3\n")
    with pytest.raises(ValueError, match="unrecognised OKX quotes header"):
        compact_okx_quotes(src, tmp_path / "x.npz", day=dt.date(2023, 1, 1))
