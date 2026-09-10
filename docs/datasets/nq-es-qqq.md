# Dataset — NQ / ES futures and QQQ options

The first `Dataset` adapter for the L1 harness. Everything here is specific to this
data and none of it is a harness constraint; the harness plan
(`docs/plans/l1-build-plan.md`) is deliberately free of all of it, and a second dataset
would bring its own version of this file.

Moved out of the v4 build plan on 2026-09-08, when the plan was split into harness and
adapter. Nothing below has been revised — it was in the wrong file, not wrong.

Generated detail lives in `docs/data-inventory.md`, `data/manifest.json` and
`config/corrections.yaml`. This document is the decision record; it does not duplicate
them.

Status markers: **[decided]** · **[open]** · **[proposed]**

## Coverage and session arithmetic

Bucket `msfe-practicum`, single prefix `raw/`, 2,491 objects, ~45 GiB. Format is
**Databento DBN under zstd, not parquet** — no footer, `read_parquet` does not apply.

**File-dates are not sessions.** Objects are split on UTC day and a CME session spans
two of them, so the session count is the one every power calculation must use. This is
the adapter's `periods()` unit.

| | objects (UTC file-dates) | **assemblable sessions** | span (sessions) |
|---|---|---|---|
| NQ | 632 | **521** | 2024-07-04 → 2026-07-10 |
| ES | 320 | **264** | 2025-07-04 → 2026-07-10 |
| QQQ options | 512 | 512 (no assembly needed) | 2024-07-03 → 2026-07-20 |

*Assemblable* = a CME session date `D` where both UTC objects it spans are present: the
`D-1` file holding the 17:00 CT evening open, and the `D` file holding the rest. The
632 → 521 drop is almost entirely a change of unit, not lost data: 110 of the 632
file-dates are evening-open-only files (105 Sundays plus 2024-12-25, 2025-01-01,
2025-01-09, 2025-12-25, 2026-01-01), which are inputs to a session rather than sessions.
Only **two** sessions are genuinely lost — 2025-04-18 (the Good Friday gap) and
2024-07-03 (the leading boundary, no 07-02 evening file). ES loses one, 2025-07-03,
for the same boundary reason.

OPRA needs no assembly: QQQ options trade 08:30–15:00 CT, inside a single UTC day.

Every count above is generated, not remembered: `scripts/inventory.py` writes them to
`data/manifest.json` under `instrument_coverage[<sym>].sessions` and
`coverage_overlap_trades_vs_opra`, renders them in `docs/data-inventory.md` §3, and the
test suite pins 521 / 264 / 505 / 255 so a change has to be deliberate.

`ohlcv_1m` deliberately excluded — bars are built from `trades`.

`raw/` intentionally diverges from `gexalgo/raw` on six objects (see Corrections).
Originals remain in the source lake.

### Symbols and the GEX intersection

**Symbols — both present, asymmetric.** NQ 521 assemblable sessions from 2024-07-04;
ES 264 from 2025-07-04. (632 and 320 are UTC file-date counts, not sessions.)

**OPRA is QQQ only** — no SPX, no NDX. **NQ has a GEX feature set, ES has none.**
Known limitation for the writeup: QQQ options are a partial view of Nasdaq-100 dealer
gamma, since NDX index options carry a large share and are absent.

**GEX-using strategies are capped by NQ ∩ OPRA at exactly 505 sessions**
(2024-07-05 → 2026-07-10), not 632 and not 521. The 16 assemblable NQ sessions that drop
out are days CME equity futures trade but US equity options are closed — Independence
Day, Labor Day, Thanksgiving, MLK, Presidents' Day, Memorial Day, Juneteenth, Good
Friday.

For ES, assemblable ∩ same-day QQQ is 255 of 264, if option (d) below becomes a
GEX-using transfer test rather than a non-GEX one.

*Implementation trap:* if a GEX feature ever needs the **prior** session's options data,
test against the previous *options trading day*, never `D-1`. Every Monday's `D-1` is a
Sunday with no OPRA file by construction, and the naive test collapses 505 → 391. On the
correct test the answer stays 505.

## Data invariants

Properties of this data that any reader of it must respect. They are enforced where
possible rather than documented — see the harness principle *make the bug
unrepresentable*.

1. **Format is Databento DBN under zstd, not parquet.** No footer; `read_parquet` does
   not apply. Use `databento` and `zstandard`.
2. **`raw/` is immutable.** Corrections are declared in `config/corrections.yaml`,
   never made by deletion. Bucket integrity is fingerprinted from listing ETags (plain
   MD5, no multipart) and checked against `data/manifest.json` on every inventory run.
3. **Symbol identity comes from the DBN header, never the path.** Six files once
   carried ES data under an NQ path. The guard is a test asserting the header/folder
   mismatch set is **empty**.
4. **Never seek past a DBN header by fixed offset.** Use the length field. v1 files
   have a 2,438-byte metadata block, v3 files 3,272. Both exist in `raw/` permanently.
5. **DBN version heterogeneity is permanent.** The mirror is v1; corrections and all
   future downloads are v3+. Both decode to the same 13-column pandas schema, so it is
   invisible above the reader — but the larger v3 metadata block is exactly why
   invariant 4 exists. `dbn_version` is recorded per object; normalize explicitly.
6. **Files split on UTC day, not CME session.** One Globex session spans two files;
   Sunday files hold only the weekly 17:00 CT open; Friday files stop at the 16:00 CT
   close. Coverage is full Globex, not RTH. Never anchor a session to a file boundary:
   anchoring VWAP there would anchor it to 18:00–19:00 CT and shift at each DST change.
7. **Timestamps are `datetime64[ns, UTC]`**, `ts_recv` index, `ts_event` column, never
   naive. CST/CDT requires explicit `tz_convert`.
8. **`opra_definition` / `opra_statistics` are double-zstd** through 2026-07-02 and
   single for the last 11 files. Unwrap in a loop until DBN magic appears.
9. **`stype_in=PARENT`** on `NQ.FUT` means `trades` files hold *all* NQ contracts, not
   the front month. Contract selection and roll are a derived-layer decision.
10. **`opra_cbbo1m` is 38 GiB.** Don't scan it. It feeds the GEX build only.
11. **`trades` is trades only** — no book or BBO, so no queue position or true spread
    cost. (`tbbo` acquisition is planned; see Data acquisition.)

## Corrections

Six files under `raw/trades/NQ/` for 2025-07-03…09 carried `ES.FUT` in their DBN
header; NQ had no data on those dates. Correct data was re-downloaded (Databento batch
`GLBX-20260908-MRU3SPA6VW`) and **written over the contaminated objects in place**.
`config/corrections.yaml` is the historical record — evidence, not a runtime
mechanism. Nothing resolves through it at read time.

*The contamination exists in the source lake too — any `gexalgo` research touching
2025-07-03 → 2025-07-09 should be re-checked.*

Known gap in the record: the originals' sha256 was never taken and cannot be recovered
from this repo. `corrections.yaml` records `sha256: null` explicitly. Recoverable from
`gexalgo/raw` if ever needed.

## Decisions

**[decided] Symbol identity comes from the DBN header, not the path.** Primary guard:
the set of `raw/trades/<SYM>/` files whose header disagrees with their folder must be
**empty**. A new contamination fails it.

**[decided] Bucket integrity is fingerprinted from listings, not downloads.** All 2,491
ETags are plain 32-hex MD5 (no multipart), so the whole bucket is verifiable at zero
egress. `inventory.py` compares live listing against the committed manifest each run;
a change on a key in `corrections.yaml` classifies as `recorded_correction`, anything
else as a critical anomaly. A test asserts the ETags stay non-composite, so a future
multipart upload fails rather than silently degrading to size-only comparison. This
replaces byte-identity with the source lake, which the overwrite gave up.

**[decided] Exclusion by declaration, never deletion** — for anything found from here
on. (The six corrections were an overwrite, decided deliberately; `raw/` is otherwise
append-only.)

**[decided] Data acquisition is Michael-only** (Databento account). Teammates cannot
re-fetch from source, so the committed manifest is the team's only integrity check —
evidence, not bookkeeping.

**[decided] Bars derived from `trades` by pinned, versioned code.**

**[decided] One bucket now; holdout partitioned before run 1.**

**[proposed] The adapter's period is a session, not a date range.**
`load_session(symbol, session_date)` assembles from two UTC-day files with tz-aware
17:00→16:00 CT boundaries. A range primitive pushes session assembly onto every caller
and they will not all agree. This is what `Dataset.periods()` and `Dataset.load()`
return for this adapter.

**[proposed] Derived layer partitioned by session date.** Session assembly happens once
at build time, not once per run. Record actual UTC session bounds per date — they shift
with DST and early closes.

**[proposed] Path envelope as this adapter's fill model.** 1m OHLC cannot resolve
which of a 25pt stop and a 100pt target was touched first, so the backtester must
assume, and the assumption becomes directional bias — structurally the same failure
that produced the +$48/trade fade artefact. Precompute, per bar, the ordered sequence
of running-extreme updates; comparing positions resolves *any* fixed bracket exactly.
Amplitude-filtered zigzag for trailing stops. Validate by replaying ~2 weeks
tick-vs-envelope and asserting identical fills.

The general form of that constraint — a fill model must resolve intra-bar ordering
rather than silently assume it — is a harness requirement and lives in the harness
plan under `FillModel`.

## Adapter build order

What building this adapter involves, once the harness reaches step 8. Blocked, in that
order, on the holdout-size decision below.

1. **Split calendar**, committed before anything else reads the data.
2. **Loader.** Session-oriented; normalizes DBN versions; never seeks by fixed offset;
   the only path to data, and it refuses anything outside the requested window.
3. **Derived layer.** Bars, path envelope, contract/roll selection; GEX if in scope.
   Built once by pinned code, SHA in the manifest.

Already done, and the reason this adapter is the first one: inventory and manifest
(`f40c3c3`), patch supersession with regression tests and CI (`601f1f3`),
post-overwrite reconciliation and bucket fingerprint (`97b6d7b`).

## Data acquisition — planned

Priority order, all within the free one-year Databento window:

1. **Extend all series to present.** `trades` stops **2026-07-10** and OPRA
   **2026-07-20** — they are not the same date. Against the CME calendar to 2026-09-08
   that is **42 missing sessions for NQ/ES** and **36 for QQQ options**.
   These are the most valuable sessions owned, because the holdout squeeze is a
   shortage of recent post-model-cutoff data. Worth a recurring monthly pull.
2. **`tbbo` for NQ.** Every trade with prevailing best bid/offer — the input for
   realistic fill costing. Upgrades the robustness auditor from "assume the trade
   price" to "you paid the spread."
3. **10–15 days of `mbo`** as a fill-model validation sample only — replay the same
   trades against true queue position to confirm the tbbo-based assumptions are
   conservative. Not for features: observations are the binding constraint, not
   features, and MBO deepens the dimension that isn't binding.
4. *Scope, not need:* SPY options (would give ES a GEX set symmetric with QQQ→NQ),
   NDX index options (would address QQQ being a partial view of Nasdaq-100 dealer
   gamma). Both large. Neither needed for the walking skeleton.

## Open questions

**[open] Holdout size vs statistical power — the binding constraint and the next
decision.** At ~one firing per day, a 3-month holdout is ~63 trades; DSR and PBO on 63
observations are weak. 505 GEX-eligible sessions cannot give both a large training set
and a statistically meaningful final holdout. Options, not exclusive:
(a) thin final holdout, lean on CPCV within train+validation, treat the holdout as
confirmatory rather than primary;
(b) longer holdout, less training data;
(c) relax one-firing-per-day to raise trade count — a pre-registered design choice, not
a response to weak power;
(d) ES's 264 sessions as a transfer test for non-GEX strategies, 255 if the test is
GEX-using — correlation ~0.95 so not clean OOS, but transfer is ADAS's anti-overfitting
defense and it turns the NQ/ES asymmetry into a second axis of evidence.

Collision worth noting: every eval window must postdate the backbone model's cutoff,
and `trades` ends 2026-07-10 — so the post-cutoff window and the natural time-ordered
holdout want to be the *same* few dozen sessions. Extending the series to present
(acquisition item 1) directly relieves this.

Note that under the harness's global-N rule this is not a per-dataset budget: trials
spent here count against the same N as trials on any other dataset.

**[open] Two calendar rules the split must pre-register.** Both change what a session
*is*, so they belong in this adapter's pre-registered calendar —
`config/calendars/nq-es-qqq.yaml`, which the v4 plan called `config/calendar.yaml`
before calendars became per-dataset — rather than being settled the first time a
strategy trips over one.

(i) **2025-01-09.** `CME_Equity` says no session — the National Day of Mourning for
Carter. The file holds 62,374 trades / 1.14 MB, against ~300k / ~7.9 MB for the
neighbouring full days and 57 KB for a genuine evening-only file like Christmas. It is a
real shortened session the calendar denies. Today it counts as evening-only and
2025-01-10 still assembles from it, so nothing breaks; treating it as tradeable needs
something beyond `CME_Equity`.

(ii) **The 16 CME-open / OPRA-closed days.** NQ sessions with a stale-GEX feature, or
excluded from GEX strategies outright? The choice moves the GEX denominator between 505
and 521.

**[open] `gex_snapshots_dedup_v4`** — copy into `derived/`, or rebuild from
`opra_cbbo1m`?

**[open] Metric, dataset-specific part.** No cross-section here, so rank IC and
long-short constructions don't apply on this data; time-series expectancy on effectively
one instrument, effective N is trades/day × days. The harness-level requirement — that
the metric be invariant to turnover, leverage and universe size — is an open question in
the harness plan.

## Falsified hypothesis families — do not re-explore

Already falsified **on this data**; pre-register as excluded and log the exclusion. They
are not falsified universally, which is why they live with the dataset rather than with
the harness. An agent spending 200 trials rediscovering these is 200 trials of wasted N.

- **GEX node attraction** as a mechanical statistical phenomenon. Tested seven ways,
  null. The ~20% touch rate is the base rate for any level at that distance.
- **Sequence models** (GRU, 1D-CNN) at current sample size — consistently underfit.
  ~267–441 positive examples is the binding constraint.
- **Two-stage confirmation architectures** — widen the effective stop, degrade
  performance. Simpler entry logic has consistently outperformed elaborate gating.

Two hard-won lessons behind those: execution artifacts masquerade as alpha (a fade
strategy showed +$48/trade across 13/13 positive months and was entirely stop-placement
artifact), and lookahead is subtle (whole-day rank as an entry gate collapsed +$83 →
−$9/trade when rewritten causally).

---

# What the adapter found (Task 8)

This document had never been executed against. `src/core/datasets/nq.py` is its
first test. Everything above held. Four things were incomplete, and one
expectation that reads naturally from it is wrong.

## Wrong: DST does not change a session's length

The document says the UTC session bounds shift with DST, which is true and
matters. The natural inference — that a session spanning a transition is 22 or 24
hours — is **not** true. **Every CME session is 23 hours**, transitions included.

The change happens at 02:00 local on a Sunday, which falls inside the weekend
halt between Friday's 16:00 CT close and Sunday's 17:00 CT open. No session ever
contains it. What moves is the UTC bound of every session — the 17:00 CT open is
22:00Z under CDT and 23:00Z under CST — which is exactly the thing that breaks
code holding a fixed offset, and exactly not a thing that changes any session's
duration. Both facts are now asserted in `tests/test_nq_adapter.py`.

## Incomplete: the `side` field encoding

Invariant 7 covers `stype_in=PARENT` and the schema is listed in
`docs/data-inventory.md`, but nothing says what the `side` column *means*. It is
the aggressor: `B` where the trade was bid-initiated, `A` where ask-initiated,
`N` where the feed does not say. The adapter maps `B → Side.BUY`, `A →
Side.SELL`, `N → None`, and every order-flow feature the agent writes will depend
on that mapping. `None` is a real value and must be handled rather than folded
into one side.

## Incomplete: the session list existed only as a count

Coverage was recorded as *521 assemblable sessions* plus the two losses. That is
not enough to build against: an adapter given only a count has to re-derive
*which* dates from a trading calendar, and the two implementations disagree on
exactly the days that matter. Deriving from file dates plus "has a predecessor"
gives **526**, because Sunday and holiday-eve objects are file dates with
predecessors and are not sessions.

`scripts/inventory.py` now publishes the list itself, at
`instrument_coverage.NQ.sessions.dates`, and the adapter reads it and cross-checks
that every listed session has both its objects. The count-only version had been
correct and unusable.

## Incomplete: 2025-07-04 is a session, not a holiday

Independence Day 2025 fell on a Friday and CME equity index futures traded a
**shortened** session, so it is a session date with an early close — the
calendar's `early_close_days` lists it. It assembles from the Thursday 17:00 CT
open to about 12:00 CT, 55,277 ticks against 247,828 for an ordinary session. The
document mentions holiday-eve files without saying that a shortened holiday
session is still a session, and it is easy to read the other way.

## Incomplete: contract selection had no stated rule

`stype_in=PARENT` is documented; what to do about it was not. Front-month
selection is a **research convention, not a fact**, so it now lives in immutable
code with the rule written down — *highest traded volume among outright contracts
in the session* — and the choice is recorded per session in
`data/nq-contract-selection.json` so a roll is visible as data rather than
inferred. Spreads are separate instruments in a PARENT file (`NQU5-NQZ5`) and are
excluded, because a spread must not win a volume comparison against the outright
it is built from.

On an ordinary session the chosen contract carries **99.9%** of session volume,
so the rule is not close to ambiguous away from a roll.

## Cost, which nothing recorded

A session is about 250,000–300,000 trades. Measured on this machine:

| | |
|---|---|
| load one session, no cache | 2.5 s |
| load one session, warm cache | 0.31 s |
| **engine walk of one session** | **~10 s** |
| end to end, per session | ~12.6 s |
| all 521 sessions, one trial | **~110 min** |

**The cache is not the bottleneck.** A warm-cache run of five sessions took 63.1 s
against 63.2 s cold — the engine walking 286,000 ticks a session dominates, at
about 22,600 ticks/s. The cache is still worth having, because it removes the
network and the DBN decode from the loop and makes repeat runs reproducible from
local bytes, but it buys about 2 s of the 12.6.

That number is the constraint on how many trials a night the loop can afford, and
it belongs to whoever designs the loop rather than to this adapter.

### What the rule does on the roll days in range

`data/nq-contract-selection.json` records all 521 sessions. **Eight rolls, one per
quarter, no flicker** — the chosen contract never returns to one it has left:

| roll session | from → to | share before | share after |
|---|---|---|---|
| 2024-09-16 | NQU4 → NQZ4 | 0.761 | 0.442 |
| 2024-12-17 | NQZ4 → NQH5 | 0.407 | 0.629 |
| 2025-03-18 | NQH5 → NQM5 | 0.438 | 0.574 |
| 2025-06-16 | NQM5 → NQU5 | 0.802 | 0.446 |
| 2025-09-16 | NQU5 → NQZ5 | 0.380 | 0.611 |
| 2025-12-15 | NQZ5 → NQH6 | 0.886 | 0.412 |
| 2026-03-16 | NQH6 → NQM6 | 0.861 | 0.407 |
| 2026-06-15 | NQM6 → NQU6 | 0.699 | 0.510 |

**The ambiguity is real and confined.** 39 of 521 sessions have the front month
below 90% of session volume, and every one of them is within four calendar days
of a roll — the Friday before through the Thursday after, five sessions per roll.
Outside those windows the minimum is 0.90 and the median 0.998.

So the rule is not close to a coin flip anywhere except in a roll week, where it
genuinely is: on 2025-09-15 the outgoing contract holds 38% and the incoming one
most of the rest. Anything sensitive to which side of a roll a session lands on
should read this file rather than assume, and a strategy whose result depends on
those 39 sessions is telling you something about the roll convention rather than
about the market.
