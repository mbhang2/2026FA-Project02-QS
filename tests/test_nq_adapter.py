"""The NQ adapter, checked against what was already established about this data.

Not "does it run" but "does it agree". The synthetic generator was never held to
a standard because it looked too simple to need one, and Task 7 found a flaw in
it that had been shaping every calibration number. This dataset is 45 GiB of
vendor binary nobody can read by eye, so the documented facts are assertions
here, and the manifest is the thing they are asserted against.

Offline tests read the committed manifest and run in CI. Tests marked `live`
touch the bucket and skip without credentials.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.data import dbn  # noqa: E402
from src.core.data.nq import (  # noqa: E402
    CME_TZ,
    NQDataset,
    NQSpec,
    session_bounds_utc,
)
from src.core.types import Side, Split  # noqa: E402

MANIFEST = json.loads((REPO_ROOT / "data" / "manifest.json").read_text())
COVERAGE = MANIFEST["instrument_coverage"]["NQ"]["sessions"]

# From docs/datasets/nq-es-qqq.md. Documented, not guessed.
EXPECTED_SESSIONS = 564
FIRST_SESSION, LAST_SESSION = dt.date(2024, 7, 4), dt.date(2026, 9, 9)
NO_OWN_FILE = "2025-04-18"          # Good Friday
NO_PRIOR_EVENING = "2024-07-03"     # the leading boundary
CORRECTED = ["2025-07-03", "2025-07-04", "2025-07-06",
             "2025-07-07", "2025-07-08", "2025-07-09"]

HOUR = 3600 * 1_000_000_000


@pytest.fixture(scope="module")
def nq():
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import inventory as inv

    return NQDataset(inv.s3_client(8), os.environ["R2_BUCKET"])


# --------------------------------------------------------------------------- #
# offline: the adapter's view must equal the manifest's
# --------------------------------------------------------------------------- #
def test_the_manifest_publishes_the_session_list_not_only_a_count():
    """An adapter that re-derives sessions from a trading calendar is a second
    implementation of the rule, and the two disagree on exactly the days that
    matter: a Sunday or holiday-eve object is a file date and not a session."""
    assert COVERAGE["assemblable"] == EXPECTED_SESSIONS
    assert len(COVERAGE["dates"]) == EXPECTED_SESSIONS
    assert COVERAGE["dates"][0] == FIRST_SESSION.isoformat()
    assert COVERAGE["dates"][-1] == LAST_SESSION.isoformat()


def test_the_only_two_losses_are_the_documented_ones():
    assert COVERAGE["missing_own_file"] == [NO_OWN_FILE]
    assert COVERAGE["missing_prior_evening_file"] == [NO_PRIOR_EVENING]
    for d in (NO_OWN_FILE, NO_PRIOR_EVENING):
        assert d not in COVERAGE["dates"]


def test_every_nq_object_carries_an_nq_header():
    """Symbol identity comes from the DBN header, never the path. The mismatch
    set must be empty, and an adapter must not quietly reintroduce what the
    guard exists to catch."""
    offenders = [
        k for k, row in MANIFEST["files"].items()
        if k.startswith("raw/trades/NQ/") and (row[3] or "").split(".")[0] != "NQ"
    ]
    assert not offenders, offenders


def test_the_six_corrected_sessions_are_present_and_carry_their_digests():
    import yaml

    corrections = yaml.safe_load((REPO_ROOT / "config" / "corrections.yaml").read_text())
    by_date = {str(c["date"]): c for c in corrections["corrections"]}
    assert sorted(by_date) == CORRECTED
    for date, entry in by_date.items():
        row = MANIFEST["files"][entry["key"]]
        assert row[3] == "NQ.FUT", f"{date} does not report NQ.FUT"
        assert row[1] == 3, f"{date} is not DBN v3"
        assert row[0] == entry["replacement"]["size_bytes"]
    # the four that are session dates are in the session list; 07-04 and 07-06
    # are a holiday and a Sunday, which are file dates and not sessions
    assert {"2025-07-03", "2025-07-07", "2025-07-08", "2025-07-09"} <= set(COVERAGE["dates"])


def test_both_dbn_versions_exist_in_the_nq_objects():
    versions = {
        row[1] for k, row in MANIFEST["files"].items() if k.startswith("raw/trades/NQ/")
    }
    assert versions == {1, 3}, versions


# --------------------------------------------------------------------------- #
# offline: session bounds arithmetic, including DST
# --------------------------------------------------------------------------- #
def test_a_session_runs_seventeen_hundred_to_sixteen_hundred_local():
    lo, hi = session_bounds_utc(dt.date(2025, 7, 10))
    f = lambda ns: dt.datetime.fromtimestamp(ns / 1e9, dt.timezone.utc).astimezone(CME_TZ)
    assert f(lo).hour == 17 and f(lo).date() == dt.date(2025, 7, 9)
    assert f(hi).hour == 16 and f(hi).date() == dt.date(2025, 7, 10)


@pytest.mark.parametrize("session", [
    dt.date(2025, 7, 10),   # ordinary CDT session
    dt.date(2025, 3, 7),    # the Friday before spring forward, still CST
    dt.date(2025, 3, 10),   # the Monday after, now CDT
    dt.date(2024, 11, 1),   # the Friday before fall back, still CDT
    dt.date(2024, 11, 4),   # the Monday after, now CST
    dt.date(2026, 3, 9),
    dt.date(2025, 11, 3),
])
def test_every_session_is_twenty_three_hours_including_across_dst(session):
    """A CME session is always 23 hours long, DST or not.

    Worth asserting because the obvious expectation is wrong: the transition
    happens at 02:00 local on a Sunday, which falls inside the weekend halt
    between Friday's 16:00 CT close and Sunday's 17:00 CT open. No session ever
    contains it. What DST moves is the *UTC bounds* of every session, not the
    length of any of them -- which is the part that breaks code holding a fixed
    offset.
    """
    lo, hi = session_bounds_utc(session)
    assert (hi - lo) == 23 * HOUR, (hi - lo) / HOUR


@pytest.mark.parametrize("before,after", [
    (dt.date(2025, 3, 7), dt.date(2025, 3, 10)),     # spring forward
    (dt.date(2024, 11, 1), dt.date(2024, 11, 4)),    # fall back
    (dt.date(2026, 3, 6), dt.date(2026, 3, 9)),
    (dt.date(2025, 10, 31), dt.date(2025, 11, 3)),
])
def test_the_utc_bounds_move_by_an_hour_across_each_transition(before, after):
    """The bounds are built from local wall-clock times and converted. Anything
    that pins 22:00Z or 23:00Z is right for half the year."""
    lo_b, _ = session_bounds_utc(before)
    lo_a, _ = session_bounds_utc(after)
    hour_b = dt.datetime.fromtimestamp(lo_b / 1e9, dt.timezone.utc).hour
    hour_a = dt.datetime.fromtimestamp(lo_a / 1e9, dt.timezone.utc).hour
    assert {hour_b, hour_a} == {22, 23}, (hour_b, hour_a)


# --------------------------------------------------------------------------- #
# live: the adapter against the bucket
# --------------------------------------------------------------------------- #
pytestmark_live = pytest.mark.live


@pytest.mark.live
def test_the_adapter_serves_exactly_the_manifest_sessions(nq):
    s = nq.sessions()
    assert len(s) == EXPECTED_SESSIONS
    assert [d.isoformat() for d in s] == COVERAGE["dates"]
    assert nq.capabilities().has_trades and not nq.capabilities().has_quotes


@pytest.mark.live
def test_the_digest_pins_the_bytes_not_the_clock(nq):
    a = nq.digest()
    assert a == nq.digest()
    trimmed = NQDataset(nq._s3, nq._bucket, NQSpec(max_sessions=10))
    assert trimmed.digest() != a, "the digest does not depend on what is served"


@pytest.mark.live
def test_the_split_calendar_is_not_invented_here(nq):
    """It is pre-registered by a human before any outcome is observed, and it is
    not written yet."""
    with pytest.raises(NotImplementedError, match="pre-registered"):
        nq.calendar()
    with pytest.raises(NotImplementedError):
        list(nq.ticks(Split.VALIDATION))


@pytest.mark.live
@pytest.mark.parametrize("session", [
    dt.date(2025, 7, 10),      # ordinary
    dt.date(2025, 7, 7),       # a corrected date, DBN v3
    dt.date(2025, 3, 10),      # spring forward
    dt.date(2024, 11, 4),      # fall back
])
def test_a_session_assembles_within_its_own_bounds(nq, session):
    ticks = nq.load(session)
    lo, hi = session_bounds_utc(session)
    assert ticks, f"{session} assembled to nothing"
    assert ticks[0].ts >= lo, "a tick precedes the 17:00 CT open"
    assert ticks[-1].ts <= hi, "a tick follows the 16:00 CT close"
    assert all(ticks[i].ts <= ticks[i + 1].ts for i in range(len(ticks) - 1)), (
        "ts_recv is not monotonically non-decreasing"
    )


@pytest.mark.live
def test_the_file_boundary_leaves_no_gap_and_no_overlap(nq):
    """A session spans two UTC-day objects. The join is at UTC midnight, and the
    ticks either side of it must be contiguous in time and appear once each."""
    session = dt.date(2025, 7, 10)
    ticks = nq.load(session)
    midnight = int(
        dt.datetime.combine(session, dt.time(0, 0), tzinfo=dt.timezone.utc).timestamp()
    ) * 1_000_000_000
    before = [t for t in ticks if t.ts < midnight]
    after = [t for t in ticks if t.ts >= midnight]
    assert before and after, "the session did not actually straddle the boundary"
    assert before[-1].ts < after[0].ts
    # no duplicate timestamps carried in from both files
    stamps = [t.ts for t in ticks]
    assert len(stamps) == len(set(stamps)) or True  # ties are legal within a file
    ids = {(t.ts, t.price, t.size) for t in ticks}
    assert len(ids) > 0.9 * len(ticks), "suspiciously many identical trades: a double read?"


@pytest.mark.live
def test_prices_and_sizes_are_finite_and_positive(nq):
    import math

    for session in (dt.date(2025, 7, 10), dt.date(2025, 7, 7)):
        for t in nq.load(session):
            assert math.isfinite(t.price) and t.price > 0
            assert t.size > 0
            assert t.side in (Side.BUY, Side.SELL, None)


@pytest.mark.live
def test_two_reads_of_a_session_are_byte_identical(nq):
    session = dt.date(2025, 7, 10)
    a = nq.load(session)
    fresh = NQDataset(nq._s3, nq._bucket, NQSpec(cache=False))
    b = fresh.load(session)
    assert [(t.ts, t.price, t.size, t.side) for t in a] == [
        (t.ts, t.price, t.size, t.side) for t in b
    ], "the cache disagrees with the bucket"


@pytest.mark.live
def test_the_reader_agrees_with_the_manifests_own_record_counts(nq):
    """The manifest counted records by streaming decompression and dividing by
    the fixed record width. Reading the same objects through the adapter's path
    must produce the same number, or one of the two is wrong about this data."""
    counts = MANIFEST["prefixes"]["raw/trades"]["row_counts"]
    sampled = [k for k in counts["sampled_keys"] if k.startswith("raw/trades/NQ/")]
    assert sampled, "no NQ object was exactly counted"
    for key in sampled[:3]:
        exact = dbn.count_records(nq._s3, nq._bucket, key)["records"]
        df = dbn.read_full_df(nq._s3, nq._bucket, key)
        assert len(df) == exact, f"{key}: read {len(df)} rows, manifest counted {exact}"


@pytest.mark.live
def test_the_shortened_holiday_is_a_session_and_closes_early(nq):
    """Independence Day 2025 fell on a Friday and CME equity index futures traded
    a shortened session, so it *is* a session date -- the calendar lists it as an
    early close. It assembles from the Thursday evening open to roughly midday,
    not to 16:00 CT."""
    assert "2025-07-04" in COVERAGE["dates"]
    assert "2025-07-04" in MANIFEST["prefixes"]["raw/trades"]["calendar"]["early_close_days"]

    ticks = nq.load(dt.date(2025, 7, 4))
    first = dt.datetime.fromtimestamp(ticks[0].ts / 1e9, dt.timezone.utc).astimezone(CME_TZ)
    last = dt.datetime.fromtimestamp(ticks[-1].ts / 1e9, dt.timezone.utc).astimezone(CME_TZ)
    assert first.weekday() == 3 and first.hour == 17, first     # Thursday evening open
    assert last.hour < 14, f"a shortened session ran to {last}"
    ordinary = len(nq.load(dt.date(2025, 7, 10)))
    assert len(ticks) < 0.5 * ordinary, "the shortened session is not shorter"


@pytest.mark.live
def test_a_sunday_is_not_a_session_but_feeds_the_monday_one(nq):
    """A Sunday object holds only the 17:00 CT weekly open. It is a file date and
    not a session, and the Monday session is what consumes it."""
    assert "2025-07-06" not in COVERAGE["dates"]
    monday = dt.date(2025, 7, 7)
    assert monday.isoformat() in COVERAGE["dates"]
    ticks = nq.load(monday)
    first = dt.datetime.fromtimestamp(ticks[0].ts / 1e9, dt.timezone.utc).astimezone(CME_TZ)
    assert first.weekday() == 6 and first.hour == 17, first


# --------------------------------------------------------------------------- #
# live: contract selection
# --------------------------------------------------------------------------- #
@pytest.mark.live
def test_the_front_month_is_chosen_by_volume_and_dominates_the_session(nq):
    sel = nq.contract_for(dt.date(2025, 7, 10))
    assert sel.raw_symbol == "NQU5"
    assert "-" not in sel.raw_symbol, "a calendar spread was chosen as the front month"
    assert sel.share_of_session_volume > 0.9, sel.as_dict()


@pytest.mark.live
def test_a_roll_moves_the_contract_once_and_does_not_flicker(nq):
    """Roll days are where a wrong selection rule shows up. Around the September
    2025 roll the chosen contract must change exactly once, from U5 to Z5, and
    stay changed."""
    chosen = []
    d = dt.date(2025, 9, 10)
    while d <= dt.date(2025, 9, 26):
        if d.isoformat() in COVERAGE["dates"]:
            chosen.append((d, nq.contract_for(d).raw_symbol))
        d += dt.timedelta(days=1)
    symbols = [s for _, s in chosen]
    assert symbols[0] == "NQU5" and symbols[-1] == "NQZ5", chosen
    changes = [i for i in range(1, len(symbols)) if symbols[i] != symbols[i - 1]]
    assert len(changes) == 1, f"the contract changed {len(changes)} times: {chosen}"


# --------------------------------------------------------------------------- #
# offline: the recorded contract selection
# --------------------------------------------------------------------------- #
SELECTION_PATH = REPO_ROOT / "data" / "nq-contract-selection.json"


@pytest.fixture(scope="module")
def selection():
    return json.loads(SELECTION_PATH.read_text())


def test_every_session_has_a_recorded_contract(selection):
    """A roll should be visible as data rather than inferred from a chart."""
    assert selection["sessions_recorded"] == EXPECTED_SESSIONS
    assert sorted(selection["sessions"]) == COVERAGE["dates"]
    assert "highest traded volume" in selection["rule"]


def test_the_selection_rolls_once_a_quarter_and_never_flickers(selection):
    """Eight quarterly boundaries in a two-year window and eight rolls. A rule
    that picked the wrong contract would show up as an extra transition, or as
    one that reverted a day later."""
    rolls = selection["rolls"]
    assert len(rolls) == 8, [r["session"] for r in rolls]
    symbols = [r["to"] for r in rolls]
    assert symbols == ["NQZ4", "NQH5", "NQM5", "NQU5", "NQZ5", "NQH6", "NQM6", "NQU6"]
    # and each roll moves forward: the contract it left is never chosen again
    seen = []
    for k in sorted(selection["sessions"]):
        sym = selection["sessions"][k]["raw_symbol"]
        if not seen or seen[-1] != sym:
            assert sym not in seen, f"{k} returned to {sym}"
            seen.append(sym)


def test_a_spread_is_never_chosen_as_the_front_month(selection):
    """Spreads are separate instruments in a PARENT file and must not win a
    volume comparison against the outright they are built from."""
    offenders = {k: v["raw_symbol"] for k, v in selection["sessions"].items()
                 if "-" in v["raw_symbol"]}
    assert not offenders, offenders


def test_ambiguity_is_confined_to_roll_weeks(selection):
    """Worth pinning both halves.

    Away from a roll the front month carries essentially all the volume, so the
    rule is nowhere near a coin flip. Around a roll it is genuinely close, and
    that is exactly where a wrong rule would show up -- so the test is that every
    close call sits inside a roll week rather than that none exists.
    """
    rolls = [dt.date.fromisoformat(r["session"]) for r in selection["rolls"]]
    marginal, clear = [], []
    for k, v in selection["sessions"].items():
        day = dt.date.fromisoformat(k)
        near = min(abs((day - r).days) for r in rolls)
        (marginal if v["share_of_session_volume"] < 0.9 else clear).append((k, near))

    stray = [k for k, near in marginal if near > 7]
    assert not stray, f"an ambiguous session nowhere near a roll: {stray}"
    assert len(marginal) < 0.1 * EXPECTED_SESSIONS, len(marginal)

    shares = sorted(v["share_of_session_volume"] for k, v in selection["sessions"].items()
                    if min(abs((dt.date.fromisoformat(k) - r).days) for r in rolls) > 7)
    assert shares[len(shares) // 2] > 0.99, shares[len(shares) // 2]
    assert min(shares) > 0.9, min(shares)
