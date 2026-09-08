"""The same guarantees, checked against the live bucket instead of the manifest.

Skipped automatically when the R2 environment is absent, so CI runs the offline
tier and a developer with `direnv` loaded gets both. Everything here is
read-only; the credential cannot write.
"""

from __future__ import annotations

import hashlib

import pytest

from test_supersessions import SUPERSEDED_DATES, header_root

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def inv():
    import inventory

    return inventory


def test_authoritative_headers_report_nq(inv, s3, bucket, supersessions):
    assert len(supersessions) == len(SUPERSEDED_DATES)
    for e in supersessions:
        key = e["superseded_by"]
        size = s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
        meta = inv.dbn_metadata(s3, bucket, key, size)
        assert header_root(meta["query_symbols"][0]) == "NQ", (
            f"{key} reports {meta['query_symbols']}"
        )
        assert meta["dataset"] == "GLBX.MDP3"
        assert meta["schema"] == "trades"


def test_patch_checksums_match_config(inv, s3, bucket, supersessions):
    for e in supersessions:
        digest = inv.sha256_of(s3, bucket, e["superseded_by"])
        assert digest == str(e["sha256"]).lower(), (
            f"{e['superseded_by']} hashes to {digest}, config says {e['sha256']}"
        )


def test_superseded_originals_are_untouched_and_still_contaminated(inv, s3, bucket, supersessions):
    """raw/ is deliberately left alone. If one of these ever starts reporting NQ,
    somebody overwrote the mirror and the byte-for-byte check against the source
    lake no longer means anything."""
    for e in supersessions:
        key = e["original"]
        size = s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
        meta = inv.dbn_metadata(s3, bucket, key, size)
        assert meta["query_symbols"] == ["ES.FUT"], (
            f"{key} now reports {meta['query_symbols']}; raw/ has been modified"
        )


def test_v1_and_v3_decode_identically(inv, s3, bucket, supersessions):
    v3_key = supersessions[0]["superseded_by"]
    v1_key = "raw/trades/NQ/glbx-mdp3-20250710.trades.dbn.zst"  # first clean NQ day after

    v1_size = s3.head_object(Bucket=bucket, Key=v1_key)["ContentLength"]
    v3_size = s3.head_object(Bucket=bucket, Key=v3_key)["ContentLength"]
    assert inv.dbn_metadata(s3, bucket, v1_key, v1_size)["dbn_version"] == 1
    assert inv.dbn_metadata(s3, bucket, v3_key, v3_size)["dbn_version"] == 3

    a = inv.read_full_df(s3, bucket, v1_key)
    b = inv.read_full_df(s3, bucket, v3_key)
    assert list(a.columns) == list(b.columns)
    assert a.dtypes.equals(b.dtypes)
    assert a.index.name == b.index.name == "ts_recv"
    assert str(a.index.dtype) == str(b.index.dtype) == "datetime64[ns, UTC]"


def test_patch_contents_are_nq_contracts(inv, s3, bucket, supersessions):
    key = [e for e in supersessions if str(e["date"]) == "2025-07-09"][0]["superseded_by"]
    df = inv.read_full_df(s3, bucket, key)
    roots = {str(sym)[:2] for sym in df["symbol"].unique()}
    assert roots == {"NQ"}, f"patch object holds non-NQ contracts: {sorted(roots)}"
