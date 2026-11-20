"""Where each object lives in the R2 bucket, separately from what it is called.

The bucket was reorganised by asset class on 2026-11-19:

    raw/trades/<SYM>/...      -> futures/raw/cme/trades/<SYM>/...
    raw/opra_cbbo1m/...       -> options/raw/opra/cbbo1m/...
    raw/opra_definition/...   -> options/raw/opra/definition/...
    raw/opra_statistics/...   -> options/raw/opra/statistics/...
    (new)                        crypto/raw/<exchange>/<kind>/<symbol>/...

**The old names stay the names.** `data/manifest.json`, `config/corrections.yaml`,
`NQDataset.digest()` and the session cache key are all built from the `raw/...`
key, and every trial ledger row records that digest; `scripts/replay.py` refuses a
row whose digest moved. Renaming the logical key would have made every recorded
trial unreproducible over a change that moved no bytes. So the `raw/...` key is
the logical identity and this table maps it to storage at the one place bytes
are fetched (`dbn.py`, and `scripts/inventory.py`'s listing and sidecar read).
Server-side copies keep the ETag (the objects are single-part, so it is the MD5),
which is what lets the identity survive the move: same name, same bytes, same
digest.

Crypto keys never had an old name, so for them the stored key is the logical key.
"""

from __future__ import annotations

# logical prefix -> stored prefix. Order matters only if one prefix contains another.
LOGICAL_TO_STORED: dict[str, str] = {
    "raw/trades/": "futures/raw/cme/trades/",
    "raw/opra_cbbo1m/": "options/raw/opra/cbbo1m/",
    "raw/opra_definition/": "options/raw/opra/definition/",
    "raw/opra_statistics/": "options/raw/opra/statistics/",
}

# Top-level roots of the reorganised bucket. `crypto/` has its own manifest
# (`data/crypto-manifest.json`) and is not part of the NQ/OPRA inventory.
ASSET_ROOTS = ("futures/", "options/", "crypto/")

# Pre-reorganisation copies, kept until every branch reads the new layout.
LEGACY_ROOT = "raw/"


def stored_key(logical: str) -> str:
    """The bucket key to fetch for a logical key. Unmapped keys are stored as named."""
    for lo, st in LOGICAL_TO_STORED.items():
        if logical.startswith(lo):
            return st + logical[len(lo):]
    return logical


def logical_key(stored: str) -> str | None:
    """The logical key for a stored one, or None if the stored key is not mapped."""
    for lo, st in LOGICAL_TO_STORED.items():
        if stored.startswith(st):
            return lo + stored[len(st):]
    return None
