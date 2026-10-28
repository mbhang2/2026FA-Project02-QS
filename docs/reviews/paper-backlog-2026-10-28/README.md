# The 25-paper backlog, worked against the deferral list — 2026-10-28

The retrieval segment has fetched 547 papers, screened 532, and produced
**2 distilled hypotheses and 1 campaign**. Eighteen distinct hypotheses have
been registered across all campaigns, so the corpus supplied 1 of 18;
everything else the loop has studied was human-written. The bottleneck is not
fetching, screening or ranking — it is the step from a ranked paper to a
registered question, and that step is human-gated by Amendment 8 on purpose.

This is that step, done by hand for the papers the pipeline already surfaced
and nobody read. **Nothing here is registered and nothing is distilled into
`config/hypotheses/`.** Reading papers is not a look (design §4) and costs
nothing in N; `global_n` is unchanged by this document.

## What the backlog actually contains

The pool is papers with `gate_relevance: high` and `feasibility` of `direct`
or `adaptable` that were never distilled. It reports as 24 entries and
contains **16 distinct titles** — eight are duplicate registry rows for the
same work (`Sequential Structure in Intraday Futures`, `The Hindsight-Selection
Trap`, `A Naive t Value Inflates`, `The Distance to the Level Ahead`, `Where
You Put the Time-Outs`, `A Sweep over Thresholds`, `A Synthetic Random Walk`,
`The Ranking of a Sweep`). Three of the 16 have no full text: both
order-flow-imbalance papers (SSRN, crossref) and the candlestick paper
(openalex). So the readable pool is **13 papers**.

**Defect for the pipeline, not for this review: there is no duplicate
detection.** The same work arrives under two ids and occupies two rows, each
separately screened and separately ranked, which spent LLM budget twice and
inflates every count in the funnel. **Corpus-wide this is 38 of 507 distinct
titles carrying more than one id, so about 40 of the 547 rows are redundant.**
Most pairs are two openalex ids for the same work (e.g. W7214001378 and
W7214017581 are both the Yamagishi *t* paper), so de-duplicating on DOI alone
would not catch them; one pair spans sources (`2605.17724v1` and
`doi-10.2139_ssrn.6792161`). See *Recommendations*.

## The deferral list

`scripts/open_questions.py` reads only campaigns with **no** validation
artefact (§4) and only seats that VETO or say CANNOT_TELL. That is 8 readable
campaigns of 48, giving 48 unresolved objections and **12 distinct**. They
cluster into three research questions and one infrastructure item:

| theme | seats | campaigns |
|---|---|---|
| **A. The headline ratio rests on too little** | statistics ×4 | nq-mvp-01 (3 trades; 1 downside session forcing the Sharpe fallback), nq-s01-continuation (69 downside sessions, skew 6.70, kurtosis 77.93) |
| **B. The result is not broad-based** | robustness ×5 | nq-s03-01/02/03 (single months dominate; removing 2025-11 flips one deeply negative), spx-arb-01 (6 of 21 hours positive, four hours carry ~4429 of a ~4414 net) |
| **C. The central construct was never built** | economics ×2 | nq-orderflow-absorption-02 (hypothesis required a level built from traded volume; code used `round(price*4)/4`, the tick grid) |
| **D. tier 3 CANNOT_TELL** | tier3 | spx-clean-01 — infrastructure, no literature bears on it |

## The mapping

### Theme A — already handled, and the paper proves it rather than opens it

**`A Naive t Value Inflates with the Number of Observations per Day`**
(Yamagishi, Zenodo F014, 2026-09-22, openalex-W7214001378) is the strongest
methodological paper in the pool and it **closes rather than opens** a
question. Its core measurement is a known-null baseline of the kind this
project runs: on a synthetic series with zero directional information by
construction, a naive *t* exceeds 2 in **0.5437** of five-minute cells, and
median |t| is 2.0893. Its rule is that *"the t values of a high-count and a
low-count method must not be compared while both are naive."*

We compare exactly such trials — spx-arb-01's 50 run 0.371 to 15.419 round
turns a session, a 41× spread — so the warning lands squarely. **But our
statistic is not the naive t.** `ScoreTuple.significance` is
`primary × sqrt(n_sessions)`, clustered on the session rather than the trade,
which is the fix the paper recommends; and the accept threshold is read off a
tabulated empirical null for the metric and session count actually in hand
(`calibration/_sweep.py`, `calibration/null.py`), not off a textbook *t*
distribution. Both of the paper's criticisms are already answered by
construction.

What survives is its *second* finding, which clustering does not fix: on the
same synthetic series the median |daily t| was **larger** on hourly bars
(1.3530) than on five-minute (0.8516), because a horizon that spans a day
leaves overlap crossing session boundaries. **That is a live question for us
only if positions are held across sessions.** Checkable from the blotters
without touching market data.

It does **not** address theme A as the statistics seat raised it. Those
objections are about small-sample instability of the ratio itself — 3 trades,
1 downside session, kurtosis 77.93 — and the paper is about correlation
between observations, a different disease. **Theme A is not answered by
anything in this backlog.**

### Theme B — one paper speaks to it directly, and offers two concrete tests

**`The Hindsight-Selection Trap: Why a Backtest Can Pass Every Standard
Robustness Check and Still Be Untradeable`** (Elangovan, Zenodo, 2026-08-02,
openalex-W7172214768) is the single highest-value item in the backlog. It
reports a pre-registered-style search over 32 candidates on SGX in which an
intraday VWAP-reversion strategy *"backtests cleanly through every standard
robustness gate — walk-forward validation, an untouched holdout period,
liquidity-capped position sizing, and a ten-item adversarial audit — yet is
provably non-tradeable."* That is our architecture's failure mode stated in
our own terms, and it is why this paper matters more than its ranking
suggested.

Two mechanical checks come out of it that we do not have:

1. **The rank-robustness test.** Their entry population was "the single most
   extreme deviation observed that session," which needs information absent at
   decision time. The edge lives entirely in the retrospectively-known rank:
   rank 1 gives +0.13% at a 51.8% win rate, rank 2 collapses to +0.003% and
   43.8% *even though rank-2 observations remain far more extreme than a random
   bar*. Re-running a candidate on the k-th extremum instead of the first is a
   **mechanical look-ahead detector** for a whole family of selection leaks.
   Our leakage check is an LLM seat reading source plus the Amendment 7 probe;
   neither would catch this, and the champion has already been caught gaming a
   byte-identity probe once.
2. **Tail-dependency, required to hold in both periods.** Their sweep produced
   a momentum signal clearing every mechanical gate — positive dev and holdout
   return, 0 of 5 negative walk-forward folds — that drew **85–97% of profit
   from three trades of several hundred, independently in development and
   holdout.** We record `profit_concentration_top5pct` and
   `best_session_share`, and the robustness seat keeps objecting on
   concentration in five of twelve deferrals while nothing resolves it. What is
   missing is not the number but a **stated bar** the seat can discharge its
   objection against, and the paper's "independently in both periods" is the
   shape of one.

Supporting, same theme, weaker: **`The Ranking of a Sweep Does Not Carry
Across Periods`** (near-zero rank correlation between selection and evaluation
periods, openalex-W7214007314) and **`A Sweep over Thresholds Cannot Produce a
Plateau in Principle`** (an apparent plateau across adjacent sweep cells is an
artifact of nested thresholds, openalex-W7214031670). Both bear on whether
parameter-neighbourhood stability means anything — a robustness argument the
committee accepts today without a measured basis.

### Theme C — nothing in the backlog, and that is the right answer

No paper in the pool mentions volume profile, point of control, value area, or
high/low-volume nodes (checked by keyword across titles, claims and adaptation
notes). This is correct and not a gap: the economics seat's objection is that
the *code* did not build the construct its own *hypothesis* required. That is a
defect report, not an open research question, and a literature search is the
wrong instrument. It is fixed by implementing a volume profile or by
withdrawing the hypothesis's claim — a decision for a human either way.

### Negative-result papers — relevant, and not to the deferrals

Four papers report that common signal families do not work: `Retail Trader's
Ruin` (five retail families fail net of cost), `Structural Limits of
OHLCV-Based Intraday Momentum Signals in MNQ` (already distilled — it is one
of the two), `Sequential Structure in Intraday Futures Data: LSTM vs Gradient
Boosting` (five-minute OHLCV lacks session-level directional structure), and
`Performance of Candlestick Analysis on Intraday Futures Data` (no full text).
Two more — `Chart Patterns Appear Just as Often on a Synthetic Random Walk` and
`A Synthetic Random Walk Has No Volatility Clustering` — are known-null
baselines of exactly our methodology.

These do not speak to any deferral. Their value is as **priors on what to stop
proposing**, which is a prompt-and-hypothesis question rather than a gate one,
and the MNQ paper being one of only two ever distilled suggests this family is
where the pipeline's judgement is already pointed.

## Recommendations

Ranked, with the Amendment 8 line held — each is a proposal for a human to
register or implement, not something this document enacts.

1. **Implement the rank-robustness test as a mechanical leakage check.** From
   the Hindsight-Selection Trap. It is the only item here that adds a
   capability the gate does not have, it is cheap, it needs no LLM gateway, and
   it targets a leak class our existing leakage apparatus demonstrably cannot
   see. It is also "deepen, not extend" exactly: a new check inside an existing
   segment.
2. **Give the robustness seat a stated concentration bar.** Five of twelve
   deferrals are this objection recurring because there is nothing to discharge
   it against. The paper's "independently in both development and holdout"
   shape is a candidate, but note that applying it to *our* holdout would be a
   look under §8, so the dev-side form is what is available now.
3. **Add duplicate detection to the registry.** Eight of 24 pool rows, and
   about 40 of 547 corpus-wide, are duplicates that were each separately
   screened and ranked by the LLM. Title normalisation is needed, not just DOI
   matching, since most pairs are two openalex ids for one work. With the
   gateway budget exhausted this is a direct saving when it is refilled, and it
   is gateway-free to build.
4. **Fetch the three missing full texts** — both order-flow-imbalance papers
   are `adaptable` and one is explicitly multiple-testing-corrected, which is
   on-topic for the gate rather than for a strategy. Fetching needs no LLM.
5. **Do not treat theme A as answerable from this corpus.** Nothing here
   addresses small-sample instability of the ratio. It is a metric question,
   pre-registered, and belongs in an amendment discussion with evidence — not
   in a literature search.

## What this document is not

It does not distil, register, or score anything, and it reports no market
result. The one paper-derived campaign to date (`nq-s05b-01`, opening-range
reversal) is unaffected. Three papers remain unread for lack of full text and
15 corpus papers remain unscreened; both are listed above rather than silently
dropped.
