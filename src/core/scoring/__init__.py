"""Scoring: the metric, its empirical null, and the calibration apparatus.

**None of this is importable from `src.strategies`.** A strategy that can compute
its own objective can optimize a number it controls, and one that can read the
null it is judged against can be written to clear it. Both prohibitions are
enforced at import time in the modules themselves, so they hold wherever the
files live.

This package deliberately re-exports **nothing**. Importing a submodule imports
its parent package first, so a convenience re-export here would run the metric's
guard on the way to `calibration` and report the wrong prohibition -- the caller
would be told it may not import the metric when what it asked for was the null.
Import the module you mean: `src.core.scoring.metrics`, `.scorer`, `.calibration`.
"""
