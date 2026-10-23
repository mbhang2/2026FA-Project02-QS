#!/usr/bin/env python3
"""Generate the data reference the RESEARCH agent is handed.

    python scripts/gen_agent_data.py            # writes docs/agent/data.md
    python scripts/gen_agent_data.py --check    # fails if it is out of date

Different reader from `CLAUDE.md` and from `docs/data-inventory.md`. `CLAUDE.md`
carries repo conventions and permission rules, for whatever edits the tree.
`docs/data-inventory.md` describes what objects sit in the bucket, for whoever
maintains the bucket. Neither answers the question a strategy author actually
has: *what can I subscribe to, what fields do I get, and what do they mean?*

Generated from `data/manifest.json` and from the dataclasses themselves rather
than written, because a hand-written data doc goes stale on the first extension
of the series and a research agent working from a stale one writes features
against fields that do not exist -- and gets an AttributeError at trial 200, or
worse, gets a plausible number.

Build-time (see `tests/test_layout.py`): this reads the committed artifacts and
the run-time types. Nothing under `src/core/` may import it.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import fields
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.core.data.dataset import calendar_path  # noqa: E402
from src.core.types import (  # noqa: E402
    SUBSCRIPTION_KINDS,
    Bar,
    Side,
    TickEvent,
)

MANIFEST = REPO_ROOT / "data" / "manifest.json"
CONTRACTS = REPO_ROOT / "data" / "nq-contract-selection.json"
OUT = REPO_ROOT / "docs" / "agent" / "data.md"

# The adapter that serves each instrument, and the dataset name it reports.
# Instruments in the bucket that no adapter serves are listed as unavailable
# rather than omitted -- "not mentioned" and "not available" read the same to an
# agent, and only one of them is true.
ADAPTERS = {
    "NQ": "nq-trades:NQ",
    "ES": None,
    "QQQ.OPT": None,
}

# One line per field, keyed by the dataclass attribute. Checked against the
# dataclass below, so adding a field to Bar or Tick without describing it here
# fails the generator instead of quietly producing an incomplete reference.
BAR_NOTES = {
    "sub_id": "Which subscription produced it (`time:300000000000`, `tick`). "
              "One callback can carry bars from several subscriptions.",
    "open_ts": "Nanoseconds UTC of the interval's **start**. This is the bar's "
               "identity — sort, join and index on it.",
    "close_ts": "Nanoseconds UTC of the interval's **end**, which is also the "
                "instant you are being called at. Both are carried so nothing "
                "downstream has to infer which one it is holding; using "
                "`open_ts` as the delivery time is how a join silently takes "
                "data from the future.",
    "open": "First trade price in the interval.",
    "high": "Highest trade price in the interval.",
    "low": "Lowest trade price in the interval.",
    "close": "Last trade price in the interval.",
    "volume": "Contracts traded in the interval. Equals "
              "`buy_volume + sell_volume + unknown_volume`.",
    "buy_volume": "Contracts in the interval whose aggressor was `Side.BUY`. "
                  "Accumulated per tick by the bar builder, so it is available "
                  "without subscribing to ticks.",
    "sell_volume": "Contracts in the interval whose aggressor was `Side.SELL`. "
                   "Ticks with an unknown aggressor are in **neither** this nor "
                   "`buy_volume` — see `unknown_volume` below.",
    "tick_count": "Number of trade events in the interval. Zero only on an "
                  "`emit_empty=True` bar, where OHLC all carry the previous "
                  "close forward.",
    "session": "Session index within the dataset, counting from 0. Bars never "
               "span a session boundary.",
}

# `TickEvent`, not the internal `Tick`: this table describes what `on_tick(ctx)`
# actually hands you as `ctx.tick`. The two carry the same fields with the same
# meanings, and documenting the one the strategy never sees would be a small lie
# that costs someone an afternoon.
TICK_NOTES = {
    "ts": "Nanoseconds UTC of the trade.",
    "seq": "Position in the walk, assigned by the engine. Ticks with equal `ts` "
           "keep their feed order through it.",
    "price": "Trade price.",
    "size": "Contracts traded.",
    "side": "Aggressor. See below — this is the field that gets silently "
            "mishandled.",
    "session": "Session index within the dataset, counting from 0.",
}


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text())


def _coverage_rows(m: dict) -> list[tuple[str, str, str, str, str]]:
    rows = []
    for sym in sorted(m["instrument_coverage"]):
        cov = m["instrument_coverage"][sym]
        s = cov["sessions"]
        dates = s.get("dates") or []
        first = dates[0] if dates else cov["first_date"]
        last = dates[-1] if dates else cov["last_date"]
        name = ADAPTERS.get(sym)
        served = f"`{name}`" if name else "**no adapter yet**"
        rows.append((sym, served, f"{s['assemblable']:,}", first, last))
    return rows


def _split_section(m: dict) -> list[str]:
    """What split this run serves. Truthful when no calendar exists, which is
    the state that matters: an agent must not be told a split it cannot get."""
    L = ["## Which split this run serves", ""]
    any_calendar = False
    for sym, name in sorted(ADAPTERS.items()):
        if name is None:
            continue
        rel = calendar_path(name)
        path = REPO_ROOT / rel
        total = m["instrument_coverage"][sym]["sessions"]["assemblable"]
        if path.exists():
            any_calendar = True
            L.append(f"- **{sym}** — `{rel}` (committed). {total:,} sessions total.")
        else:
            L.append(
                f"- **{sym}** — `{rel}` **is not written**, so no split is served "
                f"at all. All {total:,} assemblable sessions exist, and the "
                f"adapter raises rather than serving a default."
            )
    if not any_calendar:
        L += [
            "",
            "The train / validation / holdout boundaries are **pre-registered by a "
            "human before any outcome is observed**. A split chosen after looking "
            "at results is not a pre-registration, so the adapter refuses to "
            "invent one — a plausible-looking default would be worse than the "
            "error, because every number computed from it would look fine.",
            "",
            "Until a calendar is committed, a run either iterates sessions "
            "explicitly or uses the smoke path, and nothing it produces is "
            "scored.",
        ]
    else:
        L += [
            "",
            "You are given one split per run and cannot ask for another. The "
            "holdout requires an explicit unlock whose reason lands in the run "
            "record.",
        ]
    L.append("")
    return L


def _field_table(cls, notes: dict[str, str]) -> list[str]:
    got = [f.name for f in fields(cls)]
    missing = [n for n in got if n not in notes]
    extra = [n for n in notes if n not in got]
    if missing or extra:  # pragma: no cover - a wiring error, not a data state
        raise SystemExit(
            f"{cls.__name__} fields and their descriptions disagree: "
            f"undescribed={missing} described-but-absent={extra}. "
            "Update BAR_NOTES/TICK_NOTES in scripts/gen_agent_data.py."
        )
    L = ["| field | type | meaning |", "|---|---|---|"]
    for f in fields(cls):
        tp = f.type if isinstance(f.type, str) else getattr(f.type, "__name__", str(f.type))
        tp = tp.replace('"', "").replace("'", "").replace("Ts", "int (ns UTC)")
        # A union type carries a pipe, which would end the table cell.
        tp = tp.replace("|", "\\|")
        L.append(f"| `{f.name}` | `{tp}` | {notes[f.name]} |")
    return L


def render() -> str:
    m = _manifest()
    L: list[str] = []
    L += [
        "# Data available to a strategy",
        "",
        "**Generated by `scripts/gen_agent_data.py` from `data/manifest.json` and "
        "the run-time types. Do not hand-edit it — your edit will be overwritten "
        "and CI will fail first.** Change the code or regenerate after the data "
        "extends.",
        "",
        "This is the reference for writing a strategy: what you can subscribe to, "
        "what fields arrive, and what they mean. `CLAUDE.md` has the repo rules; "
        "`docs/data-inventory.md` has the bucket. Neither is this.",
        "",
        "## Datasets",
        "",
        "| instrument | adapter | sessions | first | last |",
        "|---|---|---|---|---|",
    ]
    for sym, served, n, first, last in _coverage_rows(m):
        L.append(f"| {sym} | {served} | {n} | {first} | {last} |")
    L += [
        "",
        "Sessions are **assemblable CME sessions**, not files. A session runs "
        "17:00 CT on the previous day to 16:00 CT, so it straddles exactly two "
        "UTC-day objects and a session exists only when both are present. File "
        "counts are larger and using one as N is wrong.",
        "",
        "Coverage is full Globex, not RTH.",
        "",
    ]

    L += _split_section(m)

    L += [
        "## Subscriptions",
        "",
        "Declare them in `setup(ctx)`, once, before the walk starts. You cannot "
        "subscribe later: what data a strategy consumed is part of what it is, "
        f"and it is recorded. Kinds: {', '.join(f'`{k}`' for k in SUBSCRIPTION_KINDS)}.",
        "",
        "```python",
        "def setup(self, ctx):",
        '    ctx.subscribe("5m")                    # time bars, any interval',
        '    ctx.subscribe("30s")',
        '    ctx.subscribe("1h", emit_empty=True)   # carry the last close through',
        '    ctx.subscribe("tick")                  # every trade event',
        "```",
        "",
        "- **Time bars** — any interval (`\"250ms\"`, `\"30s\"`, `\"5m\"`, `\"1h\"`, or "
        "nanoseconds as an int). Anchored to the session open, never to the wall "
        "clock, so the first bar of a session starts at its first tick. Empty "
        "intervals are skipped unless `emit_empty=True`.",
        "- **Tick events** — `ctx.subscribe(\"tick\")` delivers every trade to "
        "`on_tick(ctx)` as `ctx.tick`. This is what volume bars, dollar bars, "
        "tick imbalance and every order-flow feature are built from. It is "
        "expensive: hundreds of thousands of callbacks per session, and the "
        "wall-clock budget makes that an honest cost rather than a hidden one.",
        "",
        "Subscribing to the same interval twice returns the same subscription. "
        "When several bars close at the same instant you get **one** `on_bars` "
        "callback carrying all of them, in `ctx.closed` — never one callback per "
        "timeframe, because then the order between them would be a hidden "
        "variable that changes results and is recorded nowhere.",
        "",
        "## Bar fields",
        "",
    ]
    L += _field_table(Bar, BAR_NOTES)
    L += [
        "",
        "`bar.is_empty` is `tick_count == 0`.",
        "",
        "### Derived on the bar",
        "",
        "Two properties, not stored fields, so they cannot drift from the "
        "volumes they are computed from:",
        "",
        "| property | definition |",
        "|---|---|",
        "| `bar.delta` | `buy_volume - sell_volume` — signed order flow over the "
        "interval. |",
        "| `bar.unknown_volume` | `volume - buy_volume - sell_volume` — contracts "
        "the feed did not attribute to a side. |",
        "",
        "`delta` deliberately **excludes** unknown-aggressor volume rather than "
        "splitting or assigning it, which is the same rule the tick-level `side` "
        "section below states. `unknown_volume` is carried so you can check that "
        "the exclusion is small on your data instead of assuming it: on the NQ "
        "train split it is a single tick in the first session. If it is not small "
        "on some session, a delta feature computed there is measuring the feed's "
        "gaps, and you should say so rather than let it through.",
        "",
        "These are on every bar from every time subscription, so an order-flow "
        "feature at bar resolution does **not** need `ctx.subscribe(\"tick\")`. "
        "That matters: a tick subscription disables the engine's ability to skip "
        "spans with nothing resting in them, and on the NQ train split that is "
        "the difference between stepping 453,598 events and stepping "
        "141,578,011 — about 312x the work for the same bars. Subscribe to ticks "
        "when you need sub-bar sequence; use `bar.delta` when you do not.",
        "",
        "**`open_ts` versus `close_ts`.** A bar is stamped with its open and "
        "delivered on its close. You are called at `close_ts`; everything in the "
        "bar happened in `[open_ts, close_ts)`. Both are on the object so you "
        "never have to guess which one a downstream join is using — that guess, "
        "made wrong, is lookahead that produces a beautiful equity curve.",
        "",
        "## Tick fields",
        "",
        "What `on_tick(ctx)` hands you as `ctx.tick`:",
        "",
    ]
    L += _field_table(TickEvent, TICK_NOTES)
    L += [
        "",
        "### `side` — the one to get right",
        "",
        "The feed's aggressor flag has **three** values, and the third is real:",
        "",
        "| feed value | meaning | what you receive |",
        "|---|---|---|",
        f"| `B` | bid-initiated | `Side.BUY` (`{Side.BUY.value!r}`) |",
        f"| `A` | ask-initiated | `Side.SELL` (`{Side.SELL.value!r}`) |",
        "| `N` | **unknown — the feed does not say** | `None` |",
        "",
        "`N` must be handled. It must **never** be folded into one side, dropped "
        "silently, or defaulted to buy.",
        "",
        "It is rare — a few hundred ticks in hundreds of millions; the measured "
        "rate for this data is in `docs/datasets/nq-es-qqq.md` — and that "
        "rarity is exactly what makes it dangerous. A signed-volume feature that treats "
        "`None` as a buy will not show up in any test you write; it will show up "
        "as a small, permanent bias in every order-flow number you compute, on "
        "data you cannot re-run. Write the branch:",
        "",
        "```python",
        "if tick.side is None:",
        "    ...        # decide, explicitly, what unknown aggressor means here",
        "elif tick.side is Side.BUY:",
        "    ...",
        "else:",
        "    ...",
        "```",
        "",
        "## Causality",
        "",
        "The API is built so lookahead is hard to express rather than merely "
        "discouraged, but the rules are worth knowing before you fight them.",
        "",
        "- **`setup(ctx)`** runs once, before the walk. It may read the **train "
        "split only** — `ctx.ticks()`, `ctx.train_bars(interval)` — to fit "
        "parameters. It may not see validation or holdout, and it is the only "
        "place a subscription can be declared. Keep whatever you fitted on "
        "`self`.",
        "- **`on_bars(ctx)` / `on_tick(ctx)` / `on_fill(ctx)` / "
        "`on_lifecycle(ctx)`** run during the walk and see the present instant "
        "and the past. `ctx.history(interval)` is closed bars only; indexing at "
        "or past the cursor raises `LookaheadError` rather than returning "
        "something plausible.",
        "- **Warmup reaches backward, decisions never reach forward.** A warmup "
        "of *k* sessions lets features spin up on the *k* sessions before the "
        "split starts, so a 20-session moving average is not undefined on day "
        "one and the split does not have to donate its own head. Actions taken "
        "during warmup are ignored and counted, not executed.",
        "- **Orders are queued.** A callback records intent; the engine applies "
        "it after the callback returns. Orders placed on a bar close become "
        "eligible on the **next** event, never against the tick that closed the "
        "bar (T+1). A context is valid only inside its own callback.",
        "",
        "## What is not available",
        "",
        "The price source is **trades only**. There is no book and no quote feed, "
        "which rules out a family of features that would otherwise look "
        "available:",
        "",
        "- **No bid/ask, no BBO, no quotes** — so no spread, no mid, no "
        "microprice, no quoted-depth imbalance.",
        "- **No order book** — so no queue position, no depth, no book pressure, "
        "no cancel/replace flow.",
        "- **No vendor bars.** Bars are built here from trades, so session "
        "boundaries and anchoring live in reviewable code rather than in a "
        "vendor's convention.",
        "",
        "A fill model declares what it requires and the dataset declares what it "
        "has; a mismatch is refused at wiring time rather than half-way through a "
        "run. Do not write a feature that needs a quote and hope.",
        "",
        "## Contract selection",
        "",
    ]
    L += _contract_section(m)
    L += [
        "---",
        "",
        f"Generated from `data/manifest.json` "
        f"({m['object_count']:,} objects, schema {m['schema_version']}).",
        "",
    ]
    return "\n".join(L)


def _contract_section(m: dict) -> list[str]:
    L = [
        "The bucket is requested with `stype_in=PARENT`, which means a `trades` "
        "file holds **every** NQ contract — outrights, back months and calendar "
        "spreads — not the front month. Choosing one is a research convention, "
        "not a fact, so it is written down per session rather than inferred:",
        "",
        "- The front month is the **outright** contract with the highest traded "
        "volume in that session. Spreads (`NQU5-NQZ5`) are separate instruments "
        "in the same file and are excluded, so they cannot win the comparison.",
        "- The choice is recorded for every session in "
        "`data/nq-contract-selection.json`, so a roll is visible as data instead "
        "of being read off a chart, and a wrong rule shows up as a contract that "
        "changes on the wrong day or flickers back and forth.",
    ]
    if CONTRACTS.exists():
        sel = json.loads(CONTRACTS.read_text())
        rows = sel["sessions"] if isinstance(sel, dict) and "sessions" in sel else sel
        if isinstance(rows, dict):
            rows = list(rows.values())
        near = [r for r in rows if r.get("near_roll")]
        symbols = sorted({r["raw_symbol"] for r in rows})
        rolls = sel.get("rolls", []) if isinstance(sel, dict) else []
        L += [
            f"- {len(rows):,} sessions recorded, across {len(symbols)} contracts "
            f"({', '.join(symbols)}), with {len(rolls)} roll(s) and no flicker — "
            "the chosen contract never returns to one it has left.",
        ]
        if near:
            L += [
                f"- **`near_roll`** is set on {len(near):,} of them. "
                + str(sel.get("near_roll_rule", "")).strip()
                + " It is a flag, not an exclusion: decide for yourself whether "
                "your feature survives a session where two contracts split the "
                "volume, and say which you did. A result that depends on those "
                f"{len(near):,} sessions is telling you about the roll convention, "
                "not about the market.",
            ]
        L += [
            "",
            "Each row carries `session`, `instrument_id`, `raw_symbol`, `volume`, "
            "`trades`, `share_of_session_volume`, `near_roll` and the `rule` that "
            "chose it.",
        ]
    L.append("")
    return L


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="fail if the committed reference is out of date")
    args = ap.parse_args()
    text = render()
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print(f"{OUT.relative_to(REPO_ROOT)} is out of date; "
                  "run scripts/gen_agent_data.py", file=sys.stderr)
            return 1
        print("agent data reference is current")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(REPO_ROOT)} ({len(text.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
