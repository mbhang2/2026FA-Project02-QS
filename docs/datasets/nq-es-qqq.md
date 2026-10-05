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

Bucket `msfe-practicum`, single prefix `raw/`, 2,703 objects, ~48 GiB. Format is
**Databento DBN under zstd, not parquet** — no footer, `read_parquet` does not apply.

**File-dates are not sessions.** Objects are split on UTC day and a CME session spans
two of them, so the session count is the one every power calculation must use. This is
the adapter's `periods()` unit.

| | objects (UTC file-dates) | **assemblable sessions** | span (sessions) |
|---|---|---|---|
| NQ | 684 | **564** | 2024-07-04 → 2026-09-09 |
| ES | 372 | **307** | 2025-07-04 → 2026-09-09 |
| QQQ options | 548 | 548 (no assembly needed) | 2024-07-03 → 2026-09-09 |

Extended in two steps. `trades` first: +104 objects, taking NQ from 521 sessions to 564
and ES from 264 to 307, and leaving the options series 37 sessions short — for a few
days the two ended on different dates and the GEX intersection was pinned at 511. OPRA
followed: +108 objects across the three OPRA prefixes, 36 dates each, 2026-07-21 →
2026-09-09. **All three series now end on 2026-09-09** and the intersection is 547.

The `opra_definition` prefix lists 551 keys but holds 548 data files: `condition.json`,
`manifest.json` and `metadata.json` sit alongside them and are vendor bookkeeping. An
earlier version of the setup doc counted them as data and said 515 for what was then
512 files. Three is the tell.

*Assemblable* = a CME session date `D` where both UTC objects it spans are present: the
`D-1` file holding the 17:00 CT evening open, and the `D` file holding the rest. The
684 → 564 drop is almost entirely a change of unit, not lost data: 119 of the 684
file-dates are evening-open-only files (114 Sundays plus 2024-12-25, 2025-01-01,
2025-01-09, 2025-12-25, 2026-01-01), which are inputs to a session rather than
sessions.
Only **two** sessions are genuinely lost — 2025-04-18 (the Good Friday gap) and
2024-07-03 (the leading boundary, no 07-02 evening file). ES loses one, 2025-07-03,
for the same boundary reason.

OPRA needs no assembly: QQQ options trade 08:30–15:00 CT, inside a single UTC day.

Every count above is generated, not remembered: `scripts/inventory.py` writes them to
`data/manifest.json` under `instrument_coverage[<sym>].sessions` and
`coverage_overlap_trades_vs_opra`, renders them in `docs/data-inventory.md` §3, and the
test suite pins 564 / 307 / 547 / 297 so a change has to be deliberate.

`ohlcv_1m` deliberately excluded — bars are built from `trades`.

### The 2026-07-20 seam

The OPRA extension was delivered by a second vendor job, and the join is where an
inconsistency would sit. What was checked, and what it found:

- **The overlap is byte-identical.** 2026-07-20 exists in both jobs. The second delivery
  did not replace the first: same ETag, same size, same `last_modified`, and the
  inventory reports **0 unexplained changes on pre-existing objects** across all 2,703.
  Nothing was removed.
- **The files landed flat.** The second job first appeared under nested run-id
  directories (`raw/opra_cbbo1m/OPRA-20260910-HJ5TM9QQHP/…`) and was flattened before
  this regeneration. No nested key remains, so the one-object-per-(prefix, UTC date)
  partition still holds and the inventory's date parsing did not have to learn a second
  layout.
- **Compression joins the existing group, and there is no third regime.** All 36 new
  files per prefix are single-zstd, continuous with 2026-07-06 onward. See invariant 8.
- **Headers agree with folders.** Every new file reports `QQQ.OPT`; the
  header-versus-path mismatch set is still empty, which is the guard that caught the ES
  contamination.
- **Sizes are in distribution.** `opra_cbbo1m` medians 58.8 MiB against 80.2 for the
  earlier range — smaller, and entirely inside the historical 26–127 MiB spread.
  `opra_definition` and `opra_statistics` run 24% and 45% above their earlier medians
  with maxima barely above the previous maxima. No file is a quarter or four times the
  median of its group.

Two things the seam did *not* resolve, both worth knowing before building on the new
range:

- **`condition.json` stops at 2026-07-03.** The sidecar was not re-delivered, so the 47
  files from 2026-07-06 to 2026-09-09 — the whole single-zstd group, including
  everything from this extension — have **no vendor data-condition record**. The one
  degraded day Databento does report, 2025-10-22, is in the covered range. For the
  uncovered range the honest statement is that condition is unknown, not that it is
  good.
- **The new OPRA files are DBN v1, not v3.** New `trades` downloads arrive as v3 (NQ now
  626 v1 / 58 v3, ES 320 / 52), but all 548 OPRA files are v1. Version is a property of
  the vendor job, not of the download date, so "recent means v3" is not a rule to code
  against — read the version from the header, which is what `src/core/data/dbn.py` does.

`raw/` intentionally diverges from `gexalgo/raw` on six objects (see Corrections).
Originals remain in the source lake.

### Symbols and the GEX intersection

**Symbols — both present, asymmetric.** NQ 564 assemblable sessions from 2024-07-04;
ES 307 from 2025-07-04. (684 and 372 are UTC file-date counts, not sessions.)

**OPRA is QQQ only** — no SPX, no NDX. **NQ has a GEX feature set, ES has none.**
Known limitation for the writeup: QQQ options are a partial view of Nasdaq-100 dealer
gamma, since NDX index options carry a large share and are absent.

**GEX-using strategies are capped by NQ ∩ OPRA at exactly 547 sessions**
(2024-07-05 → 2026-09-09), not 684 and not 564. Seventeen assemblable NQ sessions drop
out, and after the OPRA extension every one of them is structural: days CME equity
futures trade but US equity options are closed — Independence Day, Labor Day,
Thanksgiving, MLK, Presidents' Day, Memorial Day, Juneteenth, Good Friday. They recur
every year and nothing recovers them. (Sixteen until 2026-09-07, Labor Day, came into
range with the extension.)

The second category is now **empty**, and keeping it empty is the thing to watch. When
`trades` was extended and OPRA was not, 37 sessions fell out of the intersection —
precisely the recent window a holdout most wants — and nothing failed, because a shorter
intersection looks exactly like a smaller dataset. `test_gex_eligible_session_counts`
now asserts the count of past-OPRA sessions is zero, so the two series drifting apart
again is a test failure rather than a quiet loss.

For ES, assemblable ∩ same-day QQQ is 297 of 307 (10 holiday mismatches), if option (d)
below becomes a GEX-using transfer test rather than a non-GEX one.

*Implementation trap:* if a GEX feature ever needs the **prior** session's options data,
test against the previous *options trading day*, never `D-1`. Every Monday's `D-1` is a
Sunday with no OPRA file by construction, and the naive test collapses the
intersection by roughly a fifth. On the correct test it is unchanged.

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
5. **DBN version heterogeneity is permanent, and the versions decode to identical
   contents.** The mirror is v1 through 2026-07-10; corrections and later downloads are
   v3. The larger v3 metadata block is exactly why invariant 4 exists. `dbn_version` is
   recorded per object; normalize explicitly.

   **Verified on contents, not schema** (Task 18). Earlier comparisons were schema-only
   and across *different* dates — same columns, same dtypes, different data. The bucket
   holds v1 for 2026-07-09 and 2026-07-10, and Databento job
   `GLBX-20260910-NVFE9V4TL4` delivered v3 for the same two dates, same instrument,
   same `stype_in=parent` request. Decoded with `DBNStore.to_df()` and compared with
   `DataFrame.equals` — index, dtypes and **every value** — over 332,792 and 303,256
   rows: **identical**. `side` takes exactly `{A, B, N}` in both.

   This could not be deferred again because the version boundary (2026-07-10 → 07-12)
   falls about a third of the way through the pre-registered holdout, 2026-06-01 →
   2026-09-09, and **the holdout is opened once**. A field that had changed meaning
   across it would have put a step change in the middle of the one measurement that
   cannot be repeated, and it would have read as a regime shift and been attributed to
   the market. The record is `data/dbn-version-equivalence.json`, pinned to the ETags of
   the objects it was verified on, so a correction in place makes it stale rather than
   leaving it vouching for different bytes.

5b. **Sessions do not all run to 16:00 CT.** Twenty-two of 564 close early, and
   `data/nq-session-closes.json` records *when* — from the exchange calendar,
   cross-checked against the observed last trade. On **five** the calendar says 12:00 CT
   and trading demonstrably continued to 12:15: the day after Thanksgiving, Christmas
   Eve, and 2025-07-03. CME equity index futures close at 12:15 CT on those half days
   and `pandas_market_calendars`' `CME_Equity` reports 12:00. The resolved close is the
   later of the two, so the session window never discards an observed trade; the
   disagreement is recorded rather than smoothed, and a CME-specific calendar is the
   real fix.

   Before this, `session_bounds` computed every session as 17:00 CT to 16:00 CT from the
   date alone, so a strategy reading `ctx.session_end` on a half day was told it had four
   hours it did not have — which stranded a position on 12 of 384 training sessions and
   cost the null pool 25 sessions to a workaround. All 384 now end flat and the pool is
   whole.
6. **Files split on UTC day, not CME session.** One Globex session spans two files;
   Sunday files hold only the weekly 17:00 CT open; Friday files stop at the 16:00 CT
   close. Coverage is full Globex, not RTH. Never anchor a session to a file boundary:
   anchoring VWAP there would anchor it to 18:00–19:00 CT and shift at each DST change.
7. **Timestamps are `datetime64[ns, UTC]`**, `ts_recv` index, `ts_event` column, never
   naive. CST/CDT requires explicit `tz_convert`.
8. **`opra_definition` / `opra_statistics` are double-zstd** through 2026-07-02 and
   single from 2026-07-06 on — now 501 files against 47, after the September extension
   landed 36 more in the single-zstd group. Unwrap in a loop until DBN magic appears.
   **Two regimes, not three:** the extension was checked for a new one and joins the
   existing single-zstd group exactly. `opra_cbbo1m` has been single-zstd for all 548
   files and never had the double layer, so the rule is per prefix, not per date.
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

**[decided] Bucket integrity is fingerprinted from listings, not downloads.** All 2,703
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

1. ~~**Extend all series to present.**~~ **Done.** `trades` to 2026-09-09 (+43 NQ
   sessions, +43 ES), then OPRA to 2026-09-09 (+36 dates across all three OPRA
   prefixes). Both series end on the same date and the GEX intersection is 547, up from
   511. Keep the monthly cadence, and **extend them together**: the few days when
   `trades` had been pulled and OPRA had not cost 37 GEX-eligible sessions and produced
   no error anywhere, because a shorter intersection is indistinguishable from a smaller
   dataset.
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
observations are weak. 547 GEX-eligible sessions cannot give both a large training set
and a statistically meaningful final holdout. Options, not exclusive:
(a) thin final holdout, lean on CPCV within train+validation, treat the holdout as
confirmatory rather than primary;
(b) longer holdout, less training data;
(c) relax one-firing-per-day to raise trade count — a pre-registered design choice, not
a response to weak power;
(d) ES's 307 sessions as a transfer test for non-GEX strategies, 297 if the test is
GEX-using — correlation ~0.95 so not clean OOS, but transfer is ADAS's anti-overfitting
defense and it turns the NQ/ES asymmetry into a second axis of evidence.

Collision worth noting: every eval window must postdate the backbone model's cutoff,
and both series end 2026-09-09 — so the post-cutoff window and the natural time-ordered
holdout want to be the *same* few dozen sessions. The extension relieved this for
`trades` and, once OPRA followed, for GEX-using strategies too: the intersection now
runs to 2026-09-09 rather than stopping seven weeks short.

Note that under the harness's global-N rule this is not a per-dataset budget: trials
spent here count against the same N as trials on any other dataset.

**[open] Two calendar rules the split must pre-register.** Both change what a session
*is*, so they belong in this adapter's pre-registered calendar —
`config/calendars/nq-trades.yaml`, which the v4 plan called `config/calendar.yaml`
before calendars became per-dataset — rather than being settled the first time a
strategy trips over one.

(i) **2025-01-09.** `CME_Equity` says no session — the National Day of Mourning for
Carter. The file holds 62,374 trades / 1.14 MB, against ~300k / ~7.9 MB for the
neighbouring full days and 57 KB for a genuine evening-only file like Christmas. It is a
real shortened session the calendar denies. Today it counts as evening-only and
2025-01-10 still assembles from it, so nothing breaks; treating it as tradeable needs
something beyond `CME_Equity`.

(ii) **The 17 CME-open / OPRA-closed days.** NQ sessions with a stale-GEX feature, or
excluded from GEX strategies outright? The choice moves the GEX denominator between 547
and 564. This is now the *only* thing separating the two numbers: the sessions that were
past the end of OPRA coverage have been filled in, so what remains is a calendar rule to
pre-register rather than data to acquire.

**[open] `gex_snapshots_dedup_v4`** — copy into `derived/`, or rebuild from
`opra_cbbo1m`?

**[open] Metric, dataset-specific part.** No cross-section here, so rank IC and
long-short constructions don't apply on this data; time-series expectancy on effectively
one instrument, effective N is trades/day × days. The harness-level requirement — that
the metric be invariant to turnover, leverage and universe size — is an open question in
the harness plan.

## 2025-04 carries a disproportionate share of every champion's profit

Observed 2026-10-05 across 21 campaigns, and it is a property of the DATA rather
than of any one strategy. Of the four campaigns whose robustness auditor vetoed
for single-month concentration, **three name 2025-04**, and they are three
unrelated questions:

| campaign | question | 2025-04's share of profit |
|---|---|---|
| `nq-t72b-03` | intraday continuation (Task 72 control arm) | **143%** |
| `nq-s05b-01` | short-side fade of the opening move | 57% |
| `nq-s06-01` | the opening range, direction left open | 44% |

The fourth, `nq-ml-02`, names 2025-10 at 59%.

143% is not a typo: that champion earned more in April 2025 than its entire net
over 18 months, so the rest of the sample lost money. A strategy whose edge is
one month is a strategy about that month.

**What this is and is not.** It is not "April 2025 is a trap to be excluded" --
removing a month because results concentrate there is fitting the calendar, and
the exposure floor and the robustness auditor already catch the shape. It is a
warning about **reading** a result: a positive Sortino on this split is more
likely to be one volatile month than an edge, and the first question to ask of
any champion here is which month it came from.

**A correction worth keeping, because it is the easy mistake.** Counting every
month any auditor mentions does NOT show this: on that count 2025-04, 2025-11 and
2025-12 tie at four mentions each, because auditors enumerate monthly P&L for
reasons other than concentration. The signal is only in the vetoes that cite a
single month's *share*. A tally of mentions would have said there is nothing here.

**Related:** Sortino is the scored metric and penalises only downside deviation,
so a few large winners in one volatile month score well on it. `nq-s06-01`'s
champion had Sortino 0.395 against Sharpe 0.143 with kurtosis 57.5 -- a 2.8x gap
is the readable signature, and it is worth computing both before believing either.

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

This document had never been executed against. `src/core/data/nq.py` is its
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

**Measured, because the rate is the whole argument.** Across the 542 NQ sessions
in the local cache — 202,852,206 ticks — `N` appears **683 times**, one tick in
297,000 (101,305,713 `B` and 101,545,810 `A`). It is far too rare to show up in
any test a strategy author writes, and far too real to ignore: folding it into
`B` puts a permanent one-sided bias into every signed-volume feature, on data
that cannot be re-run. `docs/agent/data.md` states the rule to the research
agent; this is where the number lives.

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

**Superseded by the event-driven walk (`c92b07a`).** Those figures were measured
before it. Same sessions, same blotters, measured after: ~385,000 ticks/s, 0.72 s
per session, and the full trial about **6 min** — a 17× improvement, of which 10×
came from a working-order index and the rest from the jump. The table above is
kept because the *shape* of the argument is unchanged: the engine walk, not the
cache and not the network, is what a trial costs. At 564 sessions the full trial
is about 6.8 min.

**The cache is not the bottleneck.** A warm-cache run of five sessions took 63.1 s
against 63.2 s cold — the engine walking 286,000 ticks a session dominates, at
about 22,600 ticks/s. The cache is still worth having, because it removes the
network and the DBN decode from the loop and makes repeat runs reproducible from
local bytes, but it buys about 2 s of the 12.6.

That number is the constraint on how many trials a night the loop can afford, and
it belongs to whoever designs the loop rather than to this adapter.

### What the rule does on the roll days in range

`data/nq-contract-selection.json` records all 564 sessions. **Eight rolls, one per
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

**The 2026-09 quarterly roll is not in range, and the rule has not been tested on
it.** Coverage ends 2026-09-09; the September 2026 expiry is 2026-09-18 and the volume
handover happens in the days before it, so the roll falls entirely outside the data. On
the last session NQU6 still holds 98.3% of session volume, drifting down from 99.8% two
weeks earlier — the ordinary pre-roll decay, with no sign of a handover. Eight rolls are
confirmed; the ninth will be confirmable after the next extension, and until then the
tail of this file is a single contract with no roll in it.

**The ambiguity is real and confined.** 39 of 564 sessions have the front month
below 90% of session volume, and every one of them is within four calendar days
of a roll — the Friday before through the Thursday after, five sessions per roll.
Outside those windows the minimum is 0.90 and the median 0.998. Those 39 are
flagged `near_roll` in `data/nq-contract-selection.json` so no strategy has to
re-derive the threshold; the flag is derived from `share_of_session_volume`, and
the rule is stored beside it in the same file.

So the rule is not close to a coin flip anywhere except in a roll week, where it
genuinely is: on 2025-09-15 the outgoing contract holds 38% and the incoming one
most of the rest. Anything sensitive to which side of a roll a session lands on
should read this file rather than assume, and a strategy whose result depends on
those 39 sessions is telling you something about the roll convention rather than
about the market.
