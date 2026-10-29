# Closed families

Canonical list, dataset-scoped, **do not fork**: *Falsified hypothesis families*
in `docs/datasets/nq-es-qqq.md`. `scripts/rank_papers.py` parses that section.
This file exists so a human writing the next hypothesis has the closures next
to the rest of the library.

Already closed **on NQ / ES / QQQ**, not universally:

- **GEX node attraction** as a mechanical phenomenon. The touch rate at that
  distance is the base rate.
- **Sequence models** (GRU, 1D-CNN) at current sample size. The binding limit
  is the number of positive examples, not the architecture.
- **Two-stage confirmation** (entry, then a second gate). It has widened
  effective stops and lost to simpler entries.

Re-exploring any of these spends trials on a known answer. Campaign-level
questions that did not clear the gate are *not* closed families: they live in
`log.jsonl` as `near_miss` or `campaign_index`.
