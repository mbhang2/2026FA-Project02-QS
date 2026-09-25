# nq-ml-02 — validation stage re-run, 2026-09-24 22:5x

The first `--stage validate` invocation wrote one row to `validation.jsonl` with
`outcome: harness_failed`, `wall_clock_s: None`, `fills: None`, and a
`validation.json` reading `rule_a_sortino_positive: False`.

**No look at the validation split was taken.** The strategy process never
started: `run.py`'s `_proxy_for` routed a weights-loading profile to the
ordinary `RemoteStrategy`, whose capability check refused a class that defines
`fit()` -- verbatim: *"defines fit() but the profile did not set fit_sessions"*.
The guard was correct; the routing was wrong. Fixed at `ee8e379`, and the load
half of the fitted path -- which had no end-to-end test, which is how this
reached a registered campaign -- now has one.

The two files are removed so the stage can run, per its own instruction
("delete the file deliberately if you really mean to discard the first look and
record why"). Copies are in `.scratch/overnight/discarded/`. This note is the
record. §5's one-look rule is about the data, and the data was not looked at.
