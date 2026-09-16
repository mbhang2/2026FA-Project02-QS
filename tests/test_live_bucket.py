"""The same guarantees, checked against the live bucket instead of the manifest.

Skipped automatically when the R2 environment is absent, so CI runs the offline
tier and a developer with `direnv` loaded gets both. Everything here is
read-only; the credential cannot write.
"""

from __future__ import annotations

import re

import pytest

from test_data_integrity import CORRECTED_DATES, as_date, header_root

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def inv():
    import inventory

    return inventory


@pytest.fixture(scope="module")
def live_listing(inv, s3, bucket):
    return {o["key"]: o for o in inv.list_objects(s3, bucket)}


def test_bucket_has_one_canonical_location(live_listing):
    strays = sorted(k for k in live_listing if not k.startswith("raw/"))
    assert not strays, f"objects outside raw/: {strays[:10]}"
    assert not [k for k in live_listing if k.startswith("patches/")]


def test_live_listing_matches_the_committed_manifest(manifest, live_listing):
    """The real tamper check: size and ETag straight off the bucket against what
    the repo says they were, for every object, at no egress cost."""
    assert set(live_listing) == set(manifest["files"]), (
        f"added: {sorted(set(live_listing) - set(manifest['files']))[:5]} / "
        f"removed: {sorted(set(manifest['files']) - set(live_listing))[:5]}"
    )
    drift = [
        k
        for k, o in live_listing.items()
        if [o["size"], o["etag"]] != [manifest["files"][k][0], manifest["files"][k][4]]
    ]
    assert not drift, f"{len(drift)} object(s) differ from the manifest: {drift[:10]}"


def test_corrected_objects_report_nq_v3(inv, s3, bucket, corrections):
    assert len(corrections) == len(CORRECTED_DATES)
    for e in corrections:
        key = e["key"]
        size = s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
        meta = inv.dbn_metadata(s3, bucket, key, size)
        assert header_root(meta["query_symbols"][0]) == "NQ", (
            f"{key} reports {meta['query_symbols']}"
        )
        assert meta["dbn_version"] == 3
        assert meta["dataset"] == "GLBX.MDP3"
        assert meta["schema"] == "trades"


def test_corrected_checksums_match_the_record(inv, s3, bucket, corrections):
    """Catches a revert to the contaminated bytes, or any other substitution the
    size and ETag comparison could in principle miss."""
    for e in corrections:
        digest = inv.sha256_of(s3, bucket, e["key"])
        assert digest == str(e["replacement"]["sha256"]).lower(), (
            f"{e['key']} hashes to {digest}, corrections.yaml says "
            f"{e['replacement']['sha256']}"
        )


def test_no_trades_object_disagrees_with_its_folder(inv, s3, bucket, live_listing, manifest):
    """Re-reads the DBN header of a spread of trades objects rather than trusting
    the manifest. The full sweep is what `scripts/inventory.py` does; this is a
    cheap continuous check that the manifest is not stale."""
    keys = sorted(k for k in live_listing if k.startswith("raw/trades/"))
    corrected = {e["key"] for e in manifest["corrections"]["entries"]}
    sample = sorted(set(inv.stride_sample(keys, 24)) | corrected)
    for key in sample:
        folder = key.split("/")[2]
        size = live_listing[key]["size"]
        meta = inv.dbn_metadata(s3, bucket, key, size)
        assert header_root(meta["query_symbols"][0]) == folder, (
            f"{key} reports {meta['query_symbols']}, folder claims {folder}"
        )


def test_v1_and_v3_decode_identically(inv, s3, bucket, corrections, live_listing):
    v3_key = corrections[0]["key"]
    v1_key = "raw/trades/NQ/glbx-mdp3-20250710.trades.dbn.zst"  # first clean NQ day after

    v1_size = live_listing[v1_key]["size"]
    v3_size = live_listing[v3_key]["size"]
    assert inv.dbn_metadata(s3, bucket, v1_key, v1_size)["dbn_version"] == 1
    assert inv.dbn_metadata(s3, bucket, v3_key, v3_size)["dbn_version"] == 3

    a = inv.read_full_df(s3, bucket, v1_key)
    b = inv.read_full_df(s3, bucket, v3_key)
    assert list(a.columns) == list(b.columns)
    assert a.dtypes.equals(b.dtypes)
    assert a.index.name == b.index.name == "ts_recv"
    assert str(a.index.dtype) == str(b.index.dtype) == "datetime64[ns, UTC]"


def test_corrected_object_holds_nq_contracts(inv, s3, bucket, corrections):
    key = [e for e in corrections if as_date(e["date"]) == "2025-07-09"][0]["key"]
    df = inv.read_full_df(s3, bucket, key)
    roots = {str(sym)[:2] for sym in df["symbol"].unique()}
    assert roots == {"NQ"}, f"corrected object holds non-NQ contracts: {sorted(roots)}"


def test_etags_are_plain_md5(live_listing):
    bad = [k for k, o in live_listing.items() if not re.fullmatch(r"[0-9a-f]{32}", o["etag"])]
    assert not bad, f"non-MD5 (multipart) ETags: {bad[:10]}"
