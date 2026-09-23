"""Feature libraries a strategy may import. The engine never calls them.

**Why `src/core/` and not `src/strategies/`.** Nothing here runs in the scored
path -- section 2d of `docs/design/ml-strategies.md` rejected engine-owned feature
computation and that rejection stands. This is a library: the strategy imports it
and calls it, the engine does not know it exists.

The location is about *identity*, not ownership. A helper under
`src/strategies/` would be invisible to every mechanism that says what a trial
was:

- `strategy_digest` hashes `src/strategies/current.py` and nothing else,
- `ARTIFACT_GLOBS` names that same single file,
- `SOURCE_GLOBS` is `src/core/**/*.py` and `run.py`.

So two trials with identical `current.py` and different helper code would carry
the same digest and the same fingerprint while behaving differently -- the exact
failure `store_weights` documents for weights. Under `src/core/` the code is in
`code_fingerprint`, and `ledger_check` can ask whether a row's fingerprint is
reachable in history.

It also puts these files outside `AGENT_ALLOWLIST`, so the proposing agent cannot
edit a feature definition underneath a campaign that is measuring it.
"""
