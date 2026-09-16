"""The agentic driver: plan, build, review, evaluate.

It writes `src/strategies/current.py` and invokes `run.py` as a subprocess,
importing nothing from `src.core`. That is the whole of its coupling to the
harness, and it is why a second independent driver over one interface is evidence
the interface is real rather than a shared-module habit.

Was `Gowtam/L1/` until Task 44. `schemas` and `opencode_bridge` were **top-level**
package names then -- generic enough that anything else claiming those names
would shadow or be shadowed, the same hazard Task 39 found in the two packages
called `src`. They are namespaced under this one now.
"""
