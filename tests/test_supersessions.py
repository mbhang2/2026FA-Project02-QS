"""Regressions for the NQ contamination found in Task 1 and patched in Task 1b.

These pin behaviour, not the specific incident. The point of the third test in
particular is that a *new* symbol mismatch appearing in some future mirror fails
the build, rather than being caught only because someone remembered to look.
"""

from __future__ import annotations

import datetime as dt

import pytest

SUPERSEDED_DATES = [
    "2025-07-03",
    "2025-07-04",
    "2025-07-06",
    "2025-07-07",
    "2025-07-08",
    "2025-07-09",
]

# manifest["files"] rows are [size_bytes, dbn_version, zstd_layers, dbn_header_symbol]
SIZE, VERSION, LAYERS, HEADER = range(4)


def header_root(symbol: str | None) -> str | None:
    """`NQ.FUT` -> `NQ`. The parent symbol's root is what a folder name claims."""
    return symbol.split(".")[0] if symbol else None


# --------------------------------------------------------------------------- #
# 1. the authoritative object for each patched date really is NQ
# --------------------------------------------------------------------------- #
def test_authoritative_object_for_each_patched_date_is_nq(manifest):
    resolved = manifest["instrument_coverage"]["NQ"]["authoritative_keys_for_superseded_dates"]
    assert sorted(resolved) == SUPERSEDED_DATES

    for date, entry in sorted(resolved.items()):
        assert header_root(entry["header_symbol"]) == "NQ", (
            f"{date}: authoritative object {entry['key']} reports "
            f"{entry['header_symbol']}, not NQ"
        )
        # and the object it replaces is the one that was contaminated
        assert manifest["files"][entry["replaces"]][HEADER] == "ES.FUT"


def test_patched_dates_carry_a_verified_checksum(manifest):
    for entry in manifest["supersessions"]["entries"]:
        assert entry["sha256_verified_against_bucket"] is True, (
            f"{entry['superseded_by']} does not hash to the digest in exclusions.yaml"
        )


def test_config_and_manifest_agree_on_every_supersession(manifest, supersessions):
    from_config = {
        (str(e["date"]), e["original"], e["superseded_by"], str(e["sha256"]).lower())
        for e in supersessions
    }
    from_manifest = {
        (e["date"], e["original"], e["superseded_by"], e["sha256"])
        for e in manifest["supersessions"]["entries"]
    }
    assert from_config == from_manifest


# --------------------------------------------------------------------------- #
# 2. a v1 file and a v3 file decode to the same frame
# --------------------------------------------------------------------------- #
def _trades_profiles(manifest):
    """(dbn_version -> schema block) for one sampled trades file of each version."""
    out = {}
    for prefix, d in manifest["prefixes"].items():
        if not prefix.endswith("/trades"):
            continue
        for key, prof in d["sampled_profiles"].items():
            out.setdefault(manifest["files"][key][VERSION], (key, prof["schema"]))
    return out


def test_manifest_records_both_dbn_versions(manifest):
    versions = {row[VERSION] for row in manifest["files"].values()}
    assert versions == {1, 3}, f"expected a v1 mirror and v3 patches, found {versions}"


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
# 3. no folder claims a symbol its files do not carry
# --------------------------------------------------------------------------- #
def test_every_trades_file_matches_its_folder_or_is_superseded(manifest, supersessions):
    declared = {e["original"] for e in supersessions}

    offenders = []
    for key, row in manifest["files"].items():
        parts = key.split("/")
        if len(parts) != 4 or not parts[1] == "trades":
            continue
        folder = parts[2]
        if header_root(row[HEADER]) != folder:
            offenders.append((key, row[HEADER]))

    undeclared = [(k, sym) for k, sym in offenders if k not in declared]
    assert not undeclared, (
        "trades files whose DBN header disagrees with their folder and which are not "
        "recorded in config/exclusions.yaml:\n"
        + "\n".join(f"  {k} reports {sym}" for k, sym in sorted(undeclared))
    )

    # and the six we know about are all still there, so a silent re-contamination
    # cannot be hidden by quietly deleting an exclusions entry
    assert sorted(k for k, _ in offenders) == sorted(declared)


def test_every_patch_object_is_referenced_by_a_supersession(manifest, supersessions):
    referenced = {e["superseded_by"] for e in supersessions}
    orphans = [k for k in manifest["files"] if k.startswith("patches/") and k not in referenced]
    assert not orphans, f"patch objects nothing points at: {orphans}"


# --------------------------------------------------------------------------- #
# resolved coverage
# --------------------------------------------------------------------------- #
def test_nq_coverage_after_patching(manifest):
    nq = manifest["instrument_coverage"]["NQ"]
    assert nq["usable_file_dates"] == 632
    assert nq["missing_file_dates"] == ["2025-04-18"], (
        "NQ should have exactly one gap left, Good Friday 2025-04-18"
    )
    assert nq["sources"]["patches/trades"] == 6
    assert nq["sources"]["raw/trades"] == 626


def test_es_coverage_unchanged(manifest):
    es = manifest["instrument_coverage"]["ES"]
    assert es["usable_file_dates"] == 320
    assert es["first_date"] == "2025-07-03"
    assert es["missing_file_dates"] == []
    assert "patches/trades" not in es["sources"]


def test_no_critical_anomalies_remain(manifest):
    critical = [a for a in manifest["anomalies"] if a["severity"] == "critical"]
    assert not critical, "\n".join(f"{a['kind']}: {a['detail']}" for a in critical)


def test_patch_row_counts_match_the_databento_job(manifest):
    expected = {
        "2025-07-03": 177_758,
        "2025-07-04": 51_713,
        "2025-07-06": 8_773,
        "2025-07-07": 303_542,
        "2025-07-08": 268_896,
        "2025-07-09": 281_937,
    }
    actual = {
        e["date"]: e["patch_records_exact"] for e in manifest["supersessions"]["entries"]
    }
    assert actual == expected


def test_superseded_originals_are_still_present_in_the_bucket(manifest, supersessions):
    """raw/ must stay byte-identical to the source lake: exclusion is declared,
    never enforced by deletion."""
    for e in supersessions:
        assert e["original"] in manifest["files"], (
            f"{e['original']} has been removed from raw/; the mirror is no longer "
            "byte-identical to the source lake"
        )


def test_supersession_dates_are_internally_consistent(supersessions):
    for e in supersessions:
        date = e["date"]
        date = date.isoformat() if isinstance(date, dt.date) else str(date)
        ymd = date.replace("-", "")
        assert ymd in e["original"], f"{e['original']} does not carry date {date}"
        assert ymd in e["superseded_by"], f"{e['superseded_by']} does not carry date {date}"
        assert e["original"].startswith("raw/")
        assert e["superseded_by"].startswith("patches/")
        assert len(str(e["sha256"])) == 64
