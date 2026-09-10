"""Integrity guards for `raw/`, the one canonical location.

The bucket holds exactly one object per (symbol, date) and nothing resolves a
path at read time. Two things therefore have to be true and stay true:

1. no object's DBN header disagrees with the folder that claims it -- the
   mismatch set must be *empty*, which is what catches a new contamination; and
2. nothing in `raw/` changes without a record, which the per-object size/ETag
   fingerprint in the manifest enforces.

`config/corrections.yaml` is history, not mechanism. These tests read it to
assert that the six objects it describes really do hold the corrected bytes, so
a silent revert fails the build.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest

CORRECTED_DATES = [
    "2025-07-03",
    "2025-07-04",
    "2025-07-06",
    "2025-07-07",
    "2025-07-08",
    "2025-07-09",
]

CORRECTED_ROWS = {
    "2025-07-03": 177_758,
    "2025-07-04": 51_713,
    "2025-07-06": 8_773,
    "2025-07-07": 303_542,
    "2025-07-08": 268_896,
    "2025-07-09": 281_937,
}

# manifest["files"] rows follow manifest["files_fields"]
SIZE, VERSION, LAYERS, HEADER, ETAG, MTIME = range(6)

REPO_ROOT = Path(__file__).resolve().parent.parent


def header_root(symbol: str | None) -> str | None:
    """`NQ.FUT` -> `NQ`. The parent symbol's root is what a folder name claims."""
    return symbol.split(".")[0] if symbol else None


def as_date(x) -> str:
    return x.isoformat() if isinstance(x, dt.date) else str(x)


# --------------------------------------------------------------------------- #
# 1. the primary guard: no folder claims a symbol its objects do not carry
# --------------------------------------------------------------------------- #
def test_header_folder_mismatch_set_is_empty(manifest):
    offenders = []
    for key, row in manifest["files"].items():
        parts = key.split("/")
        if len(parts) != 4 or parts[1] != "trades":
            continue
        if header_root(row[HEADER]) != parts[2]:
            offenders.append((key, row[HEADER]))

    assert not offenders, (
        "objects under raw/trades/<SYM>/ whose DBN header names a different "
        "instrument:\n" + "\n".join(f"  {k} reports {sym}" for k, sym in sorted(offenders))
    )


def test_manifest_reports_no_mismatches_per_prefix(manifest):
    for prefix, d in manifest["prefixes"].items():
        for sym, v in d["symbols"].items():
            assert v["header_symbol_mismatches"]["count"] == 0, (
                f"{prefix}/{sym}: {v['header_symbol_mismatches']}"
            )


def test_everything_lives_under_raw(manifest):
    strays = [k for k in manifest["files"] if not k.startswith("raw/")]
    assert not strays, f"objects outside raw/: {strays[:10]}"


def test_exclusions_yaml_is_gone(manifest):
    """The supersession scheme it described no longer exists; leaving the file
    around would invite someone to resolve a path through it."""
    assert not (REPO_ROOT / "config" / "exclusions.yaml").exists()
    assert (REPO_ROOT / "config" / "corrections.yaml").exists()


# --------------------------------------------------------------------------- #
# 2. the corrected objects hold the corrected bytes
# --------------------------------------------------------------------------- #
def test_corrections_cover_the_six_known_dates(corrections):
    assert sorted(as_date(e["date"]) for e in corrections) == CORRECTED_DATES


def test_corrected_objects_report_nq_v3(manifest):
    corrected = manifest["instrument_coverage"]["NQ"]["corrected_objects"]
    assert sorted(corrected) == CORRECTED_DATES

    for date, entry in sorted(corrected.items()):
        assert header_root(entry["header_symbol"]) == "NQ", (
            f"{date}: {entry['key']} reports {entry['header_symbol']}, not NQ"
        )
        assert entry["dbn_version"] == 3, f"{date}: expected DBN v3, got v{entry['dbn_version']}"
        assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"] or ""), (
            f"{date}: no recorded sha256 for the replacement"
        )


def test_corrected_objects_match_the_recorded_replacement(manifest, corrections):
    """A silent revert to the contaminated bytes changes size and header; both are
    pinned here against the config, and the ETag against the manifest."""
    by_date = {as_date(e["date"]): e for e in corrections}
    for entry in manifest["corrections"]["entries"]:
        cfg = by_date[entry["date"]]
        rep = cfg["replacement"]
        assert entry["in_bucket"] is True
        assert entry["current_header_symbol"] == rep["dbn_header_symbol"]
        assert entry["current_dbn_version"] == rep["dbn_version"]
        assert entry["current_size_bytes"] == rep["size_bytes"]
        assert entry["current_records_exact"] == rep["records"]
        assert manifest["files"][cfg["key"]][SIZE] == rep["size_bytes"]
        assert manifest["files"][cfg["key"]][HEADER] == rep["dbn_header_symbol"]


def test_corrected_row_counts_match_the_databento_job(manifest):
    actual = {e["date"]: e["current_records_exact"] for e in manifest["corrections"]["entries"]}
    assert actual == CORRECTED_ROWS


def test_corrections_record_the_original_facts(corrections):
    """The originals are gone from the bucket, so the record is the only evidence
    of what was wrong. Digests were never taken; everything else must be there."""
    for e in corrections:
        orig = e["original"]
        assert orig["dbn_header_symbol"] == "ES.FUT"
        assert orig["dbn_version"] == 1
        assert isinstance(orig["size_bytes"], int) and orig["size_bytes"] > 0
        assert orig["sha256"] is None
        assert e["found_in"] == "f40c3c3"
        assert e["databento_job"] == "GLBX-20260908-MRU3SPA6VW"


def test_divergence_from_the_source_lake_is_recorded(manifest, corrections_doc):
    div = corrections_doc["divergence_from_source_lake"]
    assert div["objects_differing"] == len(CORRECTED_DATES)
    assert div["source"] == "gexalgo/raw"
    assert div["originals_recoverable_from"]
    assert manifest["corrections"]["divergence_from_source_lake"]["source"] == "gexalgo/raw"


# --------------------------------------------------------------------------- #
# 3. per-object integrity, from listings alone
# --------------------------------------------------------------------------- #
def test_every_object_is_fingerprinted(manifest):
    assert len(manifest["files"]) == manifest["object_count"]
    assert manifest["files_fields"][SIZE] == "size_bytes"
    assert manifest["files_fields"][ETAG] == "etag"
    assert manifest["files_fields"][MTIME] == "last_modified"
    for key, row in manifest["files"].items():
        assert isinstance(row[SIZE], int) and row[SIZE] > 0, key
        assert row[ETAG], key
        assert row[MTIME], key


def test_no_composite_etags(manifest):
    """A multipart ETag is a hash of part hashes, not the object's MD5, so it is
    not comparable across re-uploads. None are expected at these object sizes."""
    composite = [k for k, row in manifest["files"].items() if "-" in row[ETAG]]
    assert not composite, f"multipart ETags found, integrity falls back to size: {composite[:10]}"
    assert manifest["integrity"]["composite_etags"] == []


def test_etags_look_like_md5(manifest):
    bad = [k for k, row in manifest["files"].items() if not re.fullmatch(r"[0-9a-f]{32}", row[ETAG])]
    assert not bad, f"ETags that are not a plain 32-hex MD5: {bad[:10]}"


def test_integrity_shows_no_unexplained_drift(manifest):
    integ = manifest["integrity"]
    unexplained = [c for c in integ["changed"] if c["classification"] != "recorded_correction"]
    assert not unexplained, (
        "objects changed since the committed manifest with no record:\n"
        + "\n".join(f"  {c['key']}: {c['changes']}" for c in unexplained)
    )
    assert not integ["removed"], f"objects removed since the committed manifest: {integ['removed']}"


# --------------------------------------------------------------------------- #
# 4. the v1/v3 mix now lives inside raw/
# --------------------------------------------------------------------------- #
def test_manifest_records_both_dbn_versions(manifest):
    versions = {row[VERSION] for row in manifest["files"].values() if row[VERSION] is not None}
    assert versions == {1, 3}, f"expected a v1/v3 mix inside raw/, found {versions}"


def _trades_profiles(manifest):
    """(dbn_version -> (key, schema)) for one sampled trades object per version."""
    out = {}
    for prefix, d in manifest["prefixes"].items():
        if not prefix.endswith("/trades"):
            continue
        for key, prof in d["sampled_profiles"].items():
            out.setdefault(manifest["files"][key][VERSION], (key, prof["schema"]))
    return out


def test_v1_and_v3_decode_to_identical_columns_and_dtypes(manifest):
    profiles = _trades_profiles(manifest)
    assert {1, 3} <= set(profiles), (
        "the manifest has no sampled trades profile for both DBN versions; "
        "re-run scripts/inventory.py"
    )
    (k1, s1), (k3, s3_) = profiles[1], profiles[3]

    assert s1["index"] == s3_["index"], f"index differs between {k1} and {k3}"
    assert [c["name"] for c in s1["columns"]] == [c["name"] for c in s3_["columns"]], (
        f"column names differ between {k1} (v1) and {k3} (v3)"
    )
    assert [c["dtype"] for c in s1["columns"]] == [c["dtype"] for c in s3_["columns"]], (
        f"column dtypes differ between {k1} (v1) and {k3} (v3)"
    )


def test_record_width_is_unchanged_across_dbn_versions(manifest):
    widths = {
        prefix: d["record_size_bytes"]
        for prefix, d in manifest["prefixes"].items()
        if prefix.endswith("/trades")
    }
    assert set(sum(widths.values(), [])) == {48}, widths


# --------------------------------------------------------------------------- #
# 5. coverage must not move
# --------------------------------------------------------------------------- #
def test_nq_coverage(manifest):
    nq = manifest["instrument_coverage"]["NQ"]
    assert nq["usable_file_dates"] == 684
    assert nq["missing_file_dates"] == ["2025-04-18"], (
        "NQ should have exactly one gap, Good Friday 2025-04-18"
    )
    assert nq["sources"] == {"raw/trades": 684}
    assert nq["historically_corrected_dates"] == CORRECTED_DATES


def test_es_coverage(manifest):
    es = manifest["instrument_coverage"]["ES"]
    assert es["usable_file_dates"] == 372
    assert es["first_date"] == "2025-07-03"
    assert es["missing_file_dates"] == []
    assert es["historically_corrected_dates"] == []


def test_no_critical_anomalies(manifest):
    critical = [a for a in manifest["anomalies"] if a["severity"] == "critical"]
    assert not critical, "\n".join(f"{a['kind']}: {a['detail']}" for a in critical)


# --------------------------------------------------------------------------- #
# 6. session counts: the denominator for every power calculation
# --------------------------------------------------------------------------- #
# File-date counts are not session counts. These pin the numbers the split
# calendar and the holdout-power arithmetic are computed from, because getting
# the denominator wrong is silent -- nothing crashes, the conclusions are just
# drawn against the wrong N.
def test_assemblable_session_counts(manifest):
    cov = manifest["instrument_coverage"]
    assert cov["NQ"]["sessions"]["assemblable"] == 564
    assert cov["ES"]["sessions"]["assemblable"] == 307
    assert cov["QQQ.OPT"]["sessions"]["assemblable"] == 512


def test_session_counts_are_not_file_date_counts(manifest):
    """The distinction is the whole point; if they ever coincide for a futures
    symbol, the session logic has silently stopped requiring the evening file."""
    for sym, objects in (("NQ", 684), ("ES", 372)):
        v = manifest["instrument_coverage"][sym]
        assert sum(v["sources"].values()) == objects
        assert v["sessions"]["assemblable"] < objects
        assert v["sessions"]["evening_only_file_dates"] > 0


def test_only_two_sessions_are_genuinely_lost(manifest):
    nq = manifest["instrument_coverage"]["NQ"]["sessions"]
    assert nq["missing_own_file"] == ["2025-04-18"]        # the Good Friday gap
    assert nq["missing_prior_evening_file"] == ["2024-07-03"]  # leading boundary
    es = manifest["instrument_coverage"]["ES"]["sessions"]
    assert es["missing_own_file"] == []
    assert es["missing_prior_evening_file"] == ["2025-07-03"]


def test_gex_eligible_session_counts(manifest):
    """511, not 512 and not 564.

    Two different reasons a futures session has no same-day options, and they must
    not be collapsed: 16 are structural (CME equity futures trade while US equity
    options are closed) and recur every year; 37 are simply past the end of OPRA
    coverage, because `trades` was extended to 2026-09-09 and OPRA was not.
    Extending OPRA recovers the second set and nothing recovers the first.
    """
    ov = manifest["coverage_overlap_trades_vs_opra"]
    last_opra = manifest["instrument_coverage"]["QQQ.OPT"]["last_date"]
    assert ov["NQ"]["QQQ.OPT"]["sessions_with_same_day_options"] == 511
    assert ov["ES"]["QQQ.OPT"]["sessions_with_same_day_options"] == 261
    for sym, structural in (("NQ", 16), ("ES", 9)):
        missing = ov[sym]["QQQ.OPT"]["futures_sessions_without_options"]
        assert len([d for d in missing if d <= last_opra]) == structural
        assert len([d for d in missing if d > last_opra]) == 37
