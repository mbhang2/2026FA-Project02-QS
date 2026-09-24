# nq-ml-01 — superseded 2026-09-24, before completion

13 of 20 trials ran, all `crashed` with `agent_blocked: static_check_unrepaired`,
`wall_clock_s = None`. The engine was never invoked on any of them.

**Cause:** the seed (`viperq_amt.py` at `bc486b98`) failed the agent's own static
check -- `N_FEATURES = len(FEATURES)` is a call at module level (E011) -- and then
mypy on four `None`-initialised attributes. Every agent edit inherited the defect,
failed, exhausted two repairs, and was recorded as crashed. The real-NQ smoke did
not catch it because the smoke bypasses the agent.

**Why superseded rather than resumed:** the record names the defective seed by
digest. Swapping `champion.py` under a registered record would make the record
describe a campaign that did not happen. The 13 rows stay as what occurred.

**What changed:** the seed is fixed and a test holds it to the checker;
`--stage register` now refuses any seed that fails the same check, before the
network preflight. Successor: `nq-ml-02`, same hypothesis, same N, corrected seed.

These 13 rows are looks that never reached the data and are recorded as such.
