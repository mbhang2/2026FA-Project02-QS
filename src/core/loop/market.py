"""What an agent is told about the market, per dataset. One source, every prompt.

The review seats, the question writer and the refiner each had NQ written into
their prompt: "NQ futures **trades** ... roughly 384 sessions", "The CME session
runs 17:00 CT". True for every campaign until the crypto adapter, and then an
actively false instruction -- the kind that gets a plausible question or review
for the wrong market rather than an error anybody sees.

So the prompts carry markers and this module fills them:

| marker | what it is |
|---|---|
| `{market_name}` | the instrument, in a phrase |
| `{market_data}` | what the data is and is not, for the review seats |
| `{market_sessions}` | the train split's session count, as a reviewer should think of it |
| `{market_have}` | the question writer's "what we have, and what we cannot do" list |

Substituted with `str.replace`, never `str.format`: the prompts contain literal
braces (`{context}`, `{}` in constraint examples) that are not markers. A marker
left unfilled is an error, not a blank.

**The market comes from the profile** (`config/run.yaml`): its dataset's `kind`
and `symbol`. A caller that does not know the profile gets NQ, which is what
every one of them was told before this existed.
"""

from __future__ import annotations

import re
from pathlib import Path

from .._roots import repo_root

DEFAULT_MARKET = "nq"

_ROOT = repo_root()
_MARKERS = ("{market_name}", "{market_data}", "{market_sessions}", "{market_have}")

_NQ = {
    "name": "NQ futures",
    "sessions": "384",
    "data": (
        "NQ futures **trades** with an aggressor side, one instrument, roughly 384\n"
        "sessions on the split a campaign sees. There is no order book, no depth, no\n"
        "bid-ask, no quotes, no other instrument, and no options data. Commission is\n"
        "charged per side and a strategy that trades constantly pays for it."),
    "have": (
        "- **One instrument**, NQ index futures, tick data: every trade with price, size and\n"
        "  the aggressor side where the feed reports it. Bars at any interval from one\n"
        "  second up, each carrying open, high, low, close, volume, trade count, and volume\n"
        "  split by aggressor (`buy_volume`, `sell_volume`, `delta`) with an\n"
        "  unknown-aggressor residual (`unknown_volume`). A running sum of bar delta is\n"
        "  cumulative volume delta; the strategy can build it bar by bar.\n"
        "- **No order book, no quotes, no depth, no other instrument, no options.**\n"
        "- **543 sessions total**, and a campaign sees a few hundred of them. A claim\n"
        "  needing thousands of independent observations, a cross-section or a panel\n"
        "  cannot be tested here -- say so and state the reduced claim that can be.\n"
        "- The strategy is event-driven: it sees each bar when it closes and may place\n"
        "  market or bracketed orders. It cannot see the future or fit outside its window.\n"
        "- Every trade pays commission and fills are modelled pessimistically. An edge\n"
        "  smaller than costs is not an edge here.\n"
        "- The CME session runs 17:00 CT the previous day to 16:00 CT; regular trading\n"
        "  hours open at 08:30 CT."),
}

_COIN = {"BTCUSDT": "BTC", "ETHUSDT": "ETH"}


def _crypto(symbol: str) -> dict:
    coin = _COIN.get(symbol, symbol.removesuffix("USDT"))
    return {
        "name": f"the Binance {symbol} perpetual future",
        "sessions": "974",
        "data": (
            f"Binance {symbol} perpetual-futures **trades** with the aggressor side on every\n"
            "trade (there is no unknown-side volume), one instrument, roughly 974 sessions\n"
            "on the split a campaign sees. A session is a UTC day: the market trades\n"
            "around the clock, with no close, no holidays and no overnight gap. There is no\n"
            "order book, no depth, no bid-ask, no quotes, no other instrument, and no\n"
            "options data. Commission is 5 basis points of notional per side, so a round\n"
            "trip costs about 0.1% and a strategy that trades constantly pays for it.\n"
            "Holding a position through a funding settlement (00:00, 08:00, 16:00 UTC)\n"
            "pays or receives the funding rate on its notional."),
        "have": (
            f"- **One instrument**, the Binance {symbol} USDT-margined perpetual future, tick\n"
            "  data: every trade with price, size in lots of 0.001 " + coin + " and the aggressor\n"
            "  side, which the exchange reports for every trade. Bars at any interval from\n"
            "  one second up, each carrying open, high, low, close, volume, trade count, and\n"
            "  volume split by aggressor (`buy_volume`, `sell_volume`, `delta`).\n"
            "  `unknown_volume` is always zero here. A running sum of bar delta is\n"
            "  cumulative volume delta; the strategy can build it bar by bar.\n"
            "- **No order book, no quotes, no depth, no other instrument, no options.**\n"
            "- **1,418 sessions total** (2023-01-01 to 2026-11-18), and a campaign sees a few\n"
            "  hundred to about a thousand of them. A claim needing a cross-section or a\n"
            "  panel cannot be tested here -- say so and state the reduced claim that can be.\n"
            "- The strategy is event-driven: it sees each bar when it closes and may place\n"
            "  market or bracketed orders. It cannot see the future or fit outside its window.\n"
            "- Every trade pays 5 basis points of notional per side and fills are modelled\n"
            "  pessimistically. Holding through a funding settlement (00:00, 08:00 and\n"
            "  16:00 UTC) pays or receives the funding rate. An edge smaller than costs is\n"
            "  not an edge here.\n"
            "- A session is a UTC day, 00:00 to 24:00 UTC. The market trades continuously:\n"
            "  there is no exchange close, no regular trading hours and no holiday. Activity\n"
            "  still varies by hour, with the Asian, European and US sessions in turn."),
    }


def _crypto_okx(symbol: str) -> dict:
    """OKX trades, with fills against the OKX book the strategy does not read."""
    base = _crypto(symbol)
    coin = symbol.removesuffix("USDT")
    swap = lambda text: text.replace("Binance", "OKX").replace("lots of 0.001", "lots of 0.0001")
    return {
        "name": swap(base["name"]),
        "sessions": "960",
        "data": swap(base["data"]).replace("roughly 974 sessions", "roughly 960 sessions").replace(
            "There is no\norder book, no depth, no bid-ask, no quotes,",
            "Your strategy does\nnot read the order book, but its fills do: market and stop orders pay the\n"
            "visible bid or ask, resting limits wait in the queue, and an order larger\n"
            "than five visible levels is refused. There is"),
        "have": swap(base["have"]).replace(
            "- **No order book, no quotes, no depth, no other instrument, no options.**",
            f"- **The strategy cannot read quotes or depth.** Fills use the OKX {coin} book.\n"
            "- **No other instrument, no options.**").replace(
            "**1,418 sessions total** (2023-01-01 to 2026-11-18)",
            "**1,403 sessions total** (2023-01-01 to 2026-11-17, less days with no book)"),
    }


def facts(market: str) -> dict:
    """The four texts for a market key (`nq`, `crypto-BTCUSDT`, ...)."""
    if market == "nq":
        return _NQ
    if market.startswith("crypto-okx-"):
        return _crypto_okx(market.split("-", 2)[2])
    if market.startswith("crypto-"):
        return _crypto(market.split("-", 1)[1])
    raise ValueError(f"unknown market {market!r}")


def market_for_profile(profile: str | None, cfg: dict | None = None) -> str:
    """The market key a profile trades, from its dataset in `config/run.yaml`."""
    if not profile:
        return DEFAULT_MARKET
    if cfg is None:
        import yaml

        from ..backtest_config import prepare_config

        cfg = prepare_config(yaml.safe_load((_ROOT / "config" / "run.yaml").read_text()))
    prof = cfg["profiles"].get(profile)
    if prof is None:
        raise KeyError(f"unknown profile {profile!r}")
    spec = cfg["datasets"].get(prof["dataset"], {})
    if spec.get("kind") == "crypto" and spec.get("book"):
        return f"crypto-okx-{spec['symbol']}"
    if spec.get("kind") == "crypto":
        return f"crypto-{spec['symbol']}"
    return DEFAULT_MARKET


def market_for_campaign(campaign_dir: Path) -> str:
    """The market a registered campaign trades, from the profile in its record."""
    import json

    rec = json.loads((Path(campaign_dir) / "campaign.json").read_text()).get("record", {})
    return market_for_profile(rec.get("profile"))


def render(prompt: str, market: str = DEFAULT_MARKET) -> str:
    """`prompt` with every market marker filled for `market`."""
    f = facts(market)
    out = (prompt.replace("{market_name}", f["name"])
           .replace("{market_data}", f["data"])
           .replace("{market_sessions}", f["sessions"])
           .replace("{market_have}", f["have"]))
    left = [m for m in _MARKERS if m in out]
    if left or re.search(r"\{market_[a-z]+\}", out):
        raise ValueError(f"unfilled market marker(s) in prompt: {left or 'unknown {market_*}'}")
    return out
