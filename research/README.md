# research/

Hypotheses, experiment scripts and analysis. **Not harness code**, and nothing
here is on the path a scored run takes.

The distinction this directory exists to hold: `docs/` documents the system,
`research/` is *input to* experiments run with it. A hypothesis is a claim
somebody wants tested; a design document is a description of the thing that
tests it. Both are prose and they are not the same kind of prose.

| | |
|---|---|
| `hypotheses/` | research hypotheses and the two templates they follow. Moved here from `nalin/l1-loop/hypotheses/` in Task 40. |
| `experiments/` | one-off scripts. `dummy_run.py` is `nalin_l1`'s end-to-end smoke; it manipulates `sys.path` to import that engine, which is what a script outside a package has to do. |

Numbers that any claim rests on do **not** live here. Those are
`docs/reviews/taskNN/`, one directory per measurement, each with the script that
produced it and a README stating the replication count and the effect size it
could resolve.
