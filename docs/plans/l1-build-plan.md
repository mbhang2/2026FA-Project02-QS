# L1 Build Plan and Data Decisions — v4

Updated 2026-09-08 after Tasks 1 (`f40c3c3`), 1b (`601f1f3`) and 1c (`97b6d7b`).
Companion to `architecture-proposal-v1.md`, which defines L1/L1.5/L2 and the
committee.

**Data-layer detail now lives in the repo** — `docs/data-inventory.md`,
`data/manifest.json`, `config/corrections.yaml`. This doc is the plan and the decision
record; it does not duplicate them.

Status markers: **[decided]** · **[open]** · **[proposed]**

## Data layer — summary

Bucket `msfe-practicum`, single prefix `raw/`, 2,491 objects, ~45 GiB. Format is
**Databento DBN under zstd, not parquet** — no footer, `read_parquet` does not apply.

**File-dates are not sessions.** Objects are split on UTC day and a CME session spans
two of them, so the session count is the one every power calculation must use.

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

## Data facts that constrain the loader

1. **DBN version heterogeneity is permanent.** Mirror is v1; corrections and all
   future downloads are v3+. Both decode to identical pandas schema, so it is
   invisible above the reader — but the **v3 metadata block is larger (3,272 vs 2,438
   bytes)**, so any code seeking past the header by fixed offset instead of by the
   length field breaks. `dbn_version` is recorded per object.
2. **Files split on UTC day, not CME session.** One Globex session spans two files;
   Sunday files hold only the weekly 17:00 CT open; Friday files stop at the 16:00 CT
   close. Coverage is full Globex, not RTH. Anchoring VWAP to a file boundary would
   anchor it to 18:00–19:00 CT and shift at each DST change.
3. **Double zstd, inconsistently.** `opra_definition` / `opra_statistics` compressed
   twice through 2026-07-02, once for the last 11 files. Unwrap in a loop until DBN
   magic appears.
4. **Timestamps unambiguous.** `datetime64[ns, UTC]`, `ts_recv` index, `ts_event`
   column, never naive. CST/CDT requires explicit `tz_convert`.
5. **`stype_in=PARENT` on `NQ.FUT`** means files contain *all* NQ contracts, not the
   front month. Contract selection and roll are a derived-layer decision.
6. **`trades` is trades only** — no book or BBO, so no queue position or true spread
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

**[proposed] The loader's primary unit is a session, not a date range.**
`load_session(symbol, session_date)` assembles from two UTC-day files with tz-aware
17:00→16:00 CT boundaries. A range primitive pushes session assembly onto every caller
and they will not all agree.

**[proposed] Derived layer partitioned by session date.** Session assembly happens once
at build time, not once per run. Record actual UTC session bounds per date — they shift
with DST and early closes.

**[proposed] Path envelope as the loop's execution layer.** 1m OHLC cannot resolve
which of a 25pt stop and a 100pt target was touched first, so the backtester must
assume, and the assumption becomes directional bias — structurally the same failure
that produced the +$48/trade fade artefact. Precompute, per bar, the ordered sequence
of running-extreme updates; comparing positions resolves *any* fixed bracket exactly.
Amplitude-filtered zigzag for trailing stops. Validate by replaying ~2 weeks
tick-vs-envelope and asserting identical fills.

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

## Resolved

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

**[open] Two calendar rules the split must pre-register.** Both change what a session
*is*, so they belong in `config/calendar.yaml` rather than being settled the first time
a strategy trips over one.

(i) **2025-01-09.** `CME_Equity` says no session — the National Day of Mourning for
Carter. The file holds 62,374 trades / 1.14 MB, against ~300k / ~7.9 MB for the
neighbouring full days and 57 KB for a genuine evening-only file like Christmas. It is a
real shortened session the calendar denies. Today it counts as evening-only and
2025-01-10 still assembles from it, so nothing breaks; treating it as tradeable needs
something beyond `CME_Equity`.

(ii) **The 16 CME-open / OPRA-closed days.** NQ sessions with a stale-GEX feature, or
excluded from GEX strategies outright? The choice moves the GEX denominator between 505
and 521.

**[open] Metric.** No cross-section, so rank IC and long-short constructions don't
apply. Time-series expectancy on effectively one instrument; effective N is
trades/day × days. Must be invariant to turnover and leverage.

**[open] `gex_snapshots_dedup_v4`** — copy into `derived/`, or rebuild from
`opra_cbbo1m`?

**[open] Python or R for the strategy layer.** Gate implementations (`purgedcv`,
`skfolio`) are Python; Peterson authored `quantstrat`, whose indicator/signal/rule
decomposition may be the expected shape. Ask directly.

## L1 build order

1. ~~Inventory and manifest~~ `f40c3c3`
1b. ~~Patch supersession, regression tests, CI~~ `601f1f3`
1c. ~~Post-overwrite reconciliation, bucket fingerprint~~ `97b6d7b`
2. **Split calendar**, committed before anything else reads the data. Blocked on the
   holdout-size decision.
3. **Loader.** Session-oriented; normalizes DBN versions; never seeks by fixed offset;
   the only path to data, and it refuses anything outside the requested window.
4. **Derived layer.** Bars, path envelope, contract/roll selection; GEX if in scope.
   Built once by pinned code, SHA in the manifest.
5. **Scorer**, in a file the agent's tooling cannot open. Tool permissions *and* a CI
   check on the diff.
6. **Trial ledger**, before the loop that fills it. Append-only per run: N, git SHA,
   hypothesis family, wall-clock, score tuple, outcome *including crashes*.
7. **Loop harness.** Propose → backtest on validation → keep or `git reset --hard` →
   log. Serial, wall-clock budgeted, `setup()` / `on_bar()` split.
8. **Agent edit surface.** One strategy module; everything else read-only.
9. **Committee as a second accept tier.** Cheap scalar inside the loop; promotion runs
   four auditors in parallel, any-one-vetoes, plus purged CV, DSR with honest N, PBO
   via CSCV, Harvey t > 3.0. Robustness auditor re-runs promoted candidates against
   tick-level fills.
10. **Null calibration.** Shuffled-label or synthetic no-signal data; confirm the gate
    rejects everything. Rejection rate on a known null is a publishable number.
11. **First real run, holdout still closed.**

## Repo layout — permission tiers

The agent may write **only** under `src/strategies/`. Enforced by tool permissions and
a CI check comparing changed paths against an allowlist.

```
config/calendar.yaml        immutable — pre-registered splits
config/corrections.yaml     historical record, not a runtime mechanism
scripts/                    run-once prep, human-only
  inventory.py probe_dbn.py
src/core/                   importable by strategies, never editable
  loader.py engine.py ledger.py
  scorer.py                 not importable by strategies either
src/strategies/current.py   THE ONLY THING THE AGENT WRITES
data/manifest.json          committed;  data/cache/ gitignored
runs/                       trial ledger, append-only JSONL, committed
tests/                      offline in CI; live tests skip without R2_*
docs/tasks/                 task briefs, versioned alongside their outputs
```

## Excluded hypothesis families

Already falsified on this data; pre-register as excluded and log the exclusion.

- GEX node attraction as a mechanical statistical phenomenon (seven methodologies,
  null; the ~20% touch rate is the base rate for any level at that distance)
- Sequence models (GRU, 1D-CNN) at current sample size — consistently underfit
- Two-stage confirmation architectures — widen the effective stop, degrade performance