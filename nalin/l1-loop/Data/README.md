# Data

Source price data lives in `raw/` and is **read-only**.

## Canonical schema

Every downstream component consumes data in one fixed shape, regardless of the
underlying source. The loader (`src/data/loader.py`) is the single adapter
boundary that produces it.

* **Index** — tz-aware UTC `DatetimeIndex` named `timestamp`, sorted ascending.
* **Columns** — `asset_id` (object), `open`, `high`, `low`, `close`, `volume`
  (all `float64`).
* **Layout** — long format: one row per `(timestamp, asset_id)`.

## Placeholder vs. real source

The current loader is a stub that reads a placeholder CSV from `raw/`. The real
data source (crypto vs. equity, tick vs. bar) is still being confirmed. Swapping
it in means reimplementing `load_prices` against the real source while keeping
the same signature and output schema — a one-file change, not a rebuild.
