"""Kept, never scored.

A second implementation by a different author is the strongest check available on
arithmetic nobody has independently verified, and Tasks 41, 42 and 43 each found
something with the one in here -- opposite sign conventions in `max_drawdown`, a
Sharpe differing by exactly sqrt(252), a fill model that fills on a bar with no
range at all, and a deflation bar computed from a pooled null that would have
reported a discovery the gate rejected.

So this is not `legacy/`, which would say superseded, nor `contrib/`, which would
say unofficial. Nothing here is on the scored path and nothing here is discarded.
"""
