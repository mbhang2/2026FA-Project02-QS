# Open questions — the corpus's positive signal

What the corpus should watch for next. This file is a human-maintained list: it
is never derived from outcomes, and it only nudges the `rerank` relevance score
that a human reads. Demotion (the negative filter, `docs/datasets/nq-es-qqq.md`)
works without it; relevance is skipped when it is absent.

- Does the shape of the closing auction absorb overnight information into the
  next open, on the NQ microstructure we trade?
- Is the direction of the next open better described by the volume imbalance at
  the close than by the close-to-open gap alone?
- At 384 train sessions, is there evidence that session-level overnight memory
  survives one-session decay, on intraday momentum beyond the debut session?
- Which share of close-to-open return, if any, is predictable from the final
  second of the regular session, on the product we actually hold?