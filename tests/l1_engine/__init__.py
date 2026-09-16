"""The second engine's own tests.

**Named `l1_engine` and not `alternatives`, and that is not cosmetic.** The first
attempt put them in `tests/alternatives/` with an `__init__.py`, which makes
`tests.alternatives` a package -- and with `tests/` on `sys.path`, `alternatives`
then resolves to *this* directory instead of the real one at the repo root, so
every `from alternatives.l1_engine import ...` fails with
`ModuleNotFoundError: No module named 'alternatives.l1_engine'`.

That is the same hazard Task 39 found in the two packages both called `src`, and
Task 44 exists partly to remove it. Reintroducing it inside `tests/` while
removing it everywhere else would have been a good joke at this project's
expense; the suite caught it on the first run.
"""
