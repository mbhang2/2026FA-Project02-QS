# Harness lessons

Facts about the metric and the loop, not about the market. This file is
**human-only**. It is never pasted into a prompt. The proposing agent sees
only the `rule_for_agent` sentences compiled from matching `harness_lesson`
rows in `log.jsonl`.

- **The objective is maximised by not trading when costs dominate and there is
  no edge.** `nq-mvp-01`: turnover fell from many thousands of trades to three;
  the search discovered the metric, not the market. Rule for the agent: do not
  raise the objective by trading less and less until almost nothing happens.
- **An agent shown its own scores learns the objective.** Task 36 history arm:
  scores rose with trial index on synthetic data with no edge. The gate still
  discarded the campaign. A rising objective across proposals is not evidence
  about the market.
- **Source edits can leave the fill log byte-identical.** `nq-ml-02`: the agent
  narrated changes that never took effect. The history whitelist now says so.
  Change what is traded, not the commentary.
- **Execution artifacts masquerade as alpha.** A fade showed a large per-trade
  figure across many positive months and was stop-placement. Lookahead is
  subtle: a whole-session rank as an entry gate collapsed once rewritten
  causally. See *Falsified hypothesis families* in `docs/datasets/nq-es-qqq.md`.
- **Better as code than as a note.** A turnover floor would re-date the metric
  (new versioned profile). A sub-period concentration diagnostic belongs in the
  campaign summary as a reported view, not in this library as a hint.
