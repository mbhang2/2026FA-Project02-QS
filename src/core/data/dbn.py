"""Reading Databento DBN objects, in one place.

The rules encoded here are documented invariants of this data, not
implementation details -- `docs/datasets/nq-es-qqq.md` is the authority:

* the objects are **DBN under zstd, not parquet**, so there is no footer;
* two of the four prefixes are zstd-compressed **twice**, so a single
  decompression yields another zstd stream rather than DBN;
* **never seek past a DBN header by a fixed offset** -- v1 metadata blocks are
  2,438 bytes and v3 are 3,272, both exist in `raw/`, and the length field is the
  only correct way past either;
* symbol identity comes from the **DBN header**, never the path.

`scripts/inventory.py` and the NQ adapter both read through this module. They used
to have separate copies of the unwrapping, which is exactly the kind of thing that
drifts until one of them is quietly wrong about a rule the other enforces.
"""

from __future__ import annotations

import hashlib
import io
from typing import Any

import databento_dbn as ddbn
import pandas as pd
import zstandard

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
DBN_MAGIC = b"DBN"

# Ranged read big enough to hold the DBN metadata block of every prefix in this
# bucket (largest observed: 386 KiB uncompressed, ~56 KiB compressed). Grown
# adaptively when a file needs more.
HEAD_BYTES = 96 * 1024


def _decompress_partial(buf: bytes, limit: int) -> bytes:
    """Decompress up to `limit` bytes, spanning frames, tolerating a truncated tail.

    Databento writes the DBN metadata block in its own zstd frame and then the
    records in a long run of ~64 KiB frames, so a single-frame decode stops
    short of the first record. A ranged read also ends mid-frame, which raises
    -- we keep whatever decoded before that.
    """
    out = io.BytesIO()
    reader = zstandard.ZstdDecompressor().stream_reader(
        io.BytesIO(buf), read_across_frames=True
    )
    try:
        while out.tell() < limit:
            chunk = reader.read(1 << 16)
            if not chunk:
                break
            out.write(chunk)
    except zstandard.ZstdError:
        pass
    return out.getvalue()


def unwrap_zstd(buf: bytes, limit: int = 1 << 21, max_layers: int = 3) -> tuple[bytes, int]:
    """Decompress until DBN magic appears.

    Two of the four prefixes in this bucket are zstd-compressed twice (the
    mirror re-compressed an already-compressed Databento file), so a single
    decompress is not enough.
    """
    layers = 0
    while buf[:4] == ZSTD_MAGIC and layers < max_layers:
        out = _decompress_partial(buf, limit)
        if not out:
            break
        buf, layers = out, layers + 1
        if buf[:3] == DBN_MAGIC:
            break
    return buf, layers


def dbn_metadata(s3, bucket: str, key: str, size: int) -> dict:
    """Read one object's DBN metadata block using ranged reads only."""
    want = HEAD_BYTES
    while True:
        end = min(want, size) - 1
        raw = s3.get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{end}")["Body"].read()
        buf, layers = unwrap_zstd(raw)
        if buf[:3] != DBN_MAGIC:
            if want >= size:
                raise ValueError(f"{key}: no DBN header found")
            want *= 4
            continue
        mlen = int.from_bytes(buf[4:8], "little")
        # +4 so the first record's length byte is in hand as well
        if len(buf) >= 8 + mlen + 4 or want >= size:
            break
        want *= 4

    m = ddbn.Metadata.decode(buf[: 8 + mlen], upgrade_policy=ddbn.VersionUpgradePolicy.AS_IS)
    # First record after the metadata block tells us the fixed record width.
    rest = buf[8 + mlen :]
    record_size = rest[0] * 4 if rest else None
    return {
        "dbn_version": buf[3],
        "compression_layers": layers,
        "dataset": str(m.dataset),
        "schema": str(m.schema),
        "stype_in": str(m.stype_in),
        "stype_out": str(m.stype_out),
        "ts_out": bool(m.ts_out),
        "query_symbols": list(m.symbols),
        "query_start_ns": int(m.start),
        "query_end_ns": int(m.end),
        "instrument_count": len(m.mappings),
        "metadata_bytes": mlen,
        "record_size": record_size,
    }


def count_records(s3, bucket: str, key: str) -> dict:
    """Exact record count for one object, streamed, O(1) memory.

    All DBN schemas used here are fixed-width, so the count follows from the
    decompressed byte count without decoding a single record. The divisibility
    check below is what makes that safe: if the file contains variable-width
    records (e.g. interleaved symbol-mapping or error messages) the remainder is
    non-zero and we say so rather than reporting a wrong number.
    """
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    dctx = zstandard.ZstdDecompressor()
    reader = dctx.stream_reader(body, read_across_frames=True)

    head = reader.read(1 << 20)
    inner_layers = 1
    if head[:4] == ZSTD_MAGIC:
        # Double-compressed prefix: rest of the outer stream is one inner stream.
        inner = io.BytesIO(head + reader.read())
        reader = zstandard.ZstdDecompressor().stream_reader(inner, read_across_frames=True)
        head = reader.read(1 << 20)
        inner_layers = 2
    if head[:3] != DBN_MAGIC:
        raise ValueError(f"{key}: no DBN header after decompression")

    mlen = int.from_bytes(head[4:8], "little")
    header_bytes = 8 + mlen
    record_size = head[header_bytes] * 4

    total = len(head)
    while True:
        chunk = reader.read(1 << 22)
        if not chunk:
            break
        total += len(chunk)

    payload = total - header_bytes
    remainder = payload % record_size
    return {
        "decompressed_bytes": total,
        "header_bytes": header_bytes,
        "record_size": record_size,
        "records": payload // record_size,
        "fixed_width_remainder": remainder,
        "compression_layers": inner_layers,
    }


def read_head_df(s3, bucket: str, key: str, max_bytes: int = 8 << 20) -> pd.DataFrame:
    """Decode the first whole records of an object from a ranged read.

    Used for `raw/opra_cbbo1m`, where a full read is 76 MB per day and the task
    forbids scanning the prefix. Enough for column names and dtypes; not enough
    for a session profile, which is why that is reported only for `trades`.
    """
    import databento as db

    raw = s3.get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{max_bytes - 1}")["Body"].read()
    buf, _ = unwrap_zstd(raw, limit=max_bytes * 8)
    mlen = int.from_bytes(buf[4:8], "little")
    header = 8 + mlen
    rec = buf[header] * 4
    whole = header + ((len(buf) - header) // rec) * rec
    return db.DBNStore.from_bytes(buf[:whole]).to_df()


def read_full_df(s3, bucket: str, key: str) -> pd.DataFrame:
    """Whole-file read to a DataFrame. Only ever called on sampled `trades` days
    and on the small `opra_definition` files."""
    import databento as db

    raw = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    buf, _ = unwrap_zstd(raw, limit=1 << 62)
    return db.DBNStore.from_bytes(buf).to_df()


def sha256_of(s3, bucket: str, key: str) -> str:
    """SHA-256 of the compressed object exactly as stored, streamed."""
    h = hashlib.sha256()
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    while True:
        chunk = body.read(1 << 22)
        if not chunk:
            break
        h.update(chunk)
    return h.hexdigest()
