# Market-making strategies — design, for review

**Status: the synthetic path and the two queue fill models exist; no quote
adapter exists, and nothing here is in force for a scored NQ campaign.**
Written 2026-10-23. The companion document for learned policies is
`docs/design/rl-strategies.md`; an RL market maker needs both.

`queue-pessimistic` and `queue-prorata` live in `src/core/engine/fills.py`. A
dataset with `quotes=True` (`src/core/data/synthetic.py`) declares `has_quotes`,
and the engine walks quotes only when the fill model requires them or the
strategy subscribed. The trades-only walk is the one the golden digests were
computed on, and it is unchanged. Buying NQ quotes, or adding a second dataset,
is still the decision in section 8.

The short version: **the harness was built to accept quote-based fill models —
`DataCapabilities` already has `has_quotes` and `has_book`, and a fill model that
requires one is refused at wiring time on a dataset that lacks it. What is missing
is a dataset that has quotes. That is a data-acquisition decision, and it is
Michael's. The engine work is moderate; the honest-fill work is where the risk is.**

---

## 1. Why the current system cannot do it

Every part of this was decided deliberately, and each is in the record:

- **The NQ adapter is trades only.** `docs/agent/data.md`: no bid, no ask, no BBO,
  no book, no queue position. The bucket's only quote feed is `opra_cbbo1m`, QQQ
  options consolidated BBO at one minute, which feeds the GEX build and says nothing
  about NQ's book.
- **The scored fill model is `unfavorable`** (`src/core/engine/fills.py`): a resting
  limit fills only when a trade prints *strictly through* it. With no book, that is
  the only defensible rule — a trade *at* your price might have been someone ahead of
  you in the queue. It means a resting order fills only when the market has already
  moved against it, which is adverse selection by construction. A market maker scored
  under it is scored on its worst fills only.
- **The paper screen names it.** `config/prompts/paper-screen.md` lists market
  making, spread capture and queue position as `infeasible` on this data.

So a strategy that posts limits around the last trade price can be written today,
but it is not market making in any sense a reviewer would accept, and its score
measures the fill model.

---

## 2. What already exists to build on

- `DataCapabilities(has_trades, has_quotes, has_book)` in `src/core/types.py`, with
  `satisfies()`.
- `FillModel.requires` and `check_capabilities` in `src/core/engine/fills.py`: a
  fill model declaring `has_quotes=True` against the NQ trades adapter raises before
  the walk. `docs/design/simulator-interface.md` designed this in Task 3 for exactly
  this case ("quote-based branches slot in when `tbbo` arrives").
- Limit orders, modify and cancel, lifecycle events and the action cap, all in
  `docs/agent/strategy-api.md`.
- The fill model and data capabilities are already recorded in every run, so a
  quote-filled row cannot be confused with a trade-filled one.

---

## 3. Data: two routes, both need a human decision

### Route A — NQ quotes, on the pre-registered sessions

Acquire an NQ quote schema from Databento for the same sessions the committed
calendar `config/calendars/nq-trades.yaml` covers. In increasing fidelity and cost:

| schema | what it gives | what it supports |
|---|---|---|
| `tbbo` | the BBO immediately before every trade | top-of-book at fill time; no queue tracking between trades |
| `bbo-1s` / `bbo-1m` | sampled BBO | spread and mid features; not fills |
| `mbp-1` | every top-of-book change, with sizes | a displayed-size queue estimate at the touch |
| `mbp-10` | ten levels | the same, plus depth for orders behind the touch |
| `mbo` | every order event | an exact queue position |

`mbp-1` is the minimum for a queue-aware fill model (section 4). File sizes must be
**measured on a sample before purchase**, not estimated. For scale,
`docs/data-inventory.md` puts the whole trades prefix, NQ and ES together, at
5.64 GiB, and `opra_cbbo1m` at 40.26 GiB. A book feed updates far more often than
trades print, so it should be expected to be the largest prefix in the bucket.

Two consequences that need deciding, not assuming:

- **Calendar.** The cleanest answer is that the quote adapter serves *exactly* the
  sessions of `config/calendars/nq-trades.yaml`, referenced by digest, so the split
  that was pre-registered on 2026-09-10 stays the split. A new calendar for a new
  schema would be a new pre-registration made after outcomes on these sessions were
  observed, which is not one.
- **N does not reset.** Global N is a union across every dataset. A quote adapter on
  the same sessions is not a fresh family.

All the invariants in `docs/datasets/nq-es-qqq.md` apply to the new files on
arrival: symbol identity from the DBN header, DBN version read from the header, a
session is two UTC-day objects, `stype_in=PARENT` holds every contract, and the
roll comes from `data/nq-contract-selection.json` so the quote adapter and the trades
adapter agree on which contract is "NQ" in each session.

### Route B — a second dataset adapter for an order-book market

The harness is dataset-agnostic; a crypto limit-order-book dataset (BTCUSDT, say)
would be a second adapter, with its own document under `docs/datasets/`, its own
calendar committed before anyone computes a return on it, and its own coverage
measured in its own units. Its N still counts against the global total.

This route is worth considering because existing RL market-making work on such data
can be run through the gate instead of re-implemented. It is more work than Route A:
a new loader, a new session definition (a 24/7 market has no CME session; one has to
be chosen and pre-registered), new invariants, and a new cost model (maker/taker fees
in basis points, not a fixed commission in index points).

---

## 4. The fill model: queue-pessimistic, and why that one

A quote-based fill model is a new research object, and the lesson of the existing
pair applies directly: **whichever model the agent is scored against is the one it
learns to exploit.** The scored model must be the pessimistic one, with an optimistic
partner kept as a diagnostic so the spread between them is visible.

Proposed scored model, on `mbp-1`, requiring `has_trades` and `has_quotes`:

1. **Joining.** A buy limit at `p` becomes eligible on the next event after it is
   placed (the existing T+1 rule). If `p` is at the best bid, it joins the **back**
   of the queue: `queue_ahead` = the displayed bid size at `p` at that instant. If
   `p` improves the best bid, `queue_ahead` = 0. If `p` is behind the touch,
   `queue_ahead` is unknown under `mbp-1` and is set when `p` becomes the touch.
2. **Advancing.** `queue_ahead` decreases only by **sell-aggressor trade volume at
   `p`**. A displayed-size decrease with no trade is a cancel, and `mbp-1` cannot
   say whether it was ahead or behind; the pessimistic choice credits none of it.
3. **Filling.** The order fills — at `p`, partially if need be — when trade volume
   at `p` exceeds `queue_ahead`, or immediately when a trade prints strictly through
   `p` (the existing `unfavorable` rule, which remains a sufficient condition).
4. **Not modelled, and stated:** the order's own effect on the displayed book and
   on other participants; hidden and implied liquidity; the exchange's matching
   algorithm beyond price-time.

The diagnostic partner credits cancels pro rata. A candidate whose edge collapses
between the two is telling you its edge is in the cancel assumption.

**Latency.** T+1 is "the next event", which on a quote feed can be microseconds.
A fixed, pre-registered order-entry latency — eligible at the first event at or after
`ts + L` — is the honest version. **`L` is a pre-registration value, not a tunable,**
and it is not chosen here: choosing it after seeing results is the
threshold-tuning failure `CLAUDE.md` warns about.

**Verification before trust.** On a sample of sessions, compare `tbbo`-based,
`mbp-1`-based and (if ever acquired) `mbo`-based fills for the same order stream,
and report the disagreement. State the replication count and the effect size the
comparison can resolve before running it.

---

## 5. The economics, which should be read before any engine work

At the registered cost, **capturing one full NQ tick of spread on a round turn
leaves 0.05 index points**: the tick is 0.25, and commission is 0.10 a side under
`nq-train` in `config/run.yaml`. That is before adverse selection, before the idle
charge, and before any queue that does not fill. A market maker on NQ at this
commission needs fills that are mostly not adversely selected, which is exactly what
a pessimistic queue model will not grant cheaply.

That is not a reason not to build it. It is a reason to run a **positive control
first**, in the style of `scripts/positive_control.py`: a synthetic quote dataset with
a planted spread-capture edge, to establish that the harness can detect a real market
maker at this cost before a rejection on NQ is read as a finding.

---

## 6. What changes in the code, if adopted

Ordered so that each step is testable before the next depends on it.

1. **Types.** A frozen `Quote` value type in `src/core/types.py` (timestamp, bid
   price and size, ask price and size, session), and a `quote` subscription kind
   beside `time` and `tick`.
2. **Synthetic quotes.** `src/core/data/synthetic.py` gains a quote generator and
   declares `has_quotes=True` when it is on, so the fill model and the positive
   control can be tested offline in CI.
3. **Fill models.** The queue-pessimistic model and its diagnostic partner in
   `src/core/engine/fills.py`, each declaring `requires`. Order state gains
   `queue_ahead`.
4. **Engine walk.** Quote events interleaved with trades in timestamp order, with
   feed order preserved at equal timestamps as ticks are now. The span-skipping
   optimisation that lets a bar strategy step 453,598 events instead of 141,578,011
   must be re-derived for resting orders at the touch; per-trial cost will rise and
   must be measured, not guessed.
5. **The remote surface.** `ctx.quote` crosses to the child, so
   `src/core/engine/remote/_surface.py` is regenerated by
   `scripts/gen_remote_surface.py`.
6. **Agent documents.** `docs/agent/data.md` is generated by
   `scripts/gen_agent_data.py` from the adapter's capabilities; its "What is not
   available" section must stop saying "no quotes" for a quote dataset and keep
   saying it for the trades one. `docs/agent/strategy-api.md` gains the quote
   callback and the queue semantics a strategy needs to reason about its fills.
7. **The adapter**, Route A or B, after the data exists.

**Unchanged, deliberately:** the metric, the idle charge, the bootstrap block length,
the null tables, the golden digests, and the `unfavorable` model's semantics for
trades-only datasets.

---

## 7. What the gate does and does not already cover

- **The metric needs no change.** A market maker produces round turns and a session
  P&L series like any other strategy.
- **The null's calibration does not carry over.** `round_turn_mc`
  (`src/core/scoring/randomization/round_turn.py`) sign-flips each round turn's P&L.
  Its family-wise error rate is still being measured, on trades-filled candidates.
  A market maker has thousands of small, correlated round turns, and a different fill
  model is a different configuration; until the error rate is measured on one, no
  market-making campaign carries a calibrated false-positive rate. That is a
  statement to put in the campaign record, not a reason to skip it.
- **Turnover is the expected failure mode.** The search drifted toward not trading on
  `nq-mvp-01`; a market-making search can drift the other way, toward turnover the
  queue model rewards and the market would not. The economics auditor's prompt
  already names "turnover the instrument cannot absorb"; quote data at least makes
  that checkable.

---

## 8. Open, for Brian and Michael

- **Route A or Route B**, and for A, which schema. Michael holds the Databento account,
  so this is his call on cost as well as on scope.
- **The order-entry latency `L`**, pre-registered before the first quote-filled run.
- **Whether a quote-based campaign is a new hypothesis family**, corrected separately
  at L2, or a trial-level change inside an existing one. Section 5 of
  `docs/design/ml-strategies.md` is the precedent: if the hypothesis names the fill
  mechanism, each mechanism is its own campaign.
