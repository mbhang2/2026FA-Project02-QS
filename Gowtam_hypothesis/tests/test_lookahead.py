"""Lookahead lint: false positives must pass, real lookahead must stay blocked.

The BAD list is a ratchet: any spec a reviewer finds with real lookahead gets added
here, so loosening the lint can never silently reopen it.
"""

import pytest

from researcher.schemas import find_lookahead

# (text, allow_fill) that are causal and must NOT be flagged.
GOOD = [
    ("Rolling futures basis = F_t - S_t; z-score over 20 bars", False),
    ("fair_value_futures = spot_close_t * exp(carry)", False),
    ("MA_fast_t = mean(C_{t-n_fast+1:t}); signal_t = 1 if MA_fast_t > MA_slow_t else -1", False),
    ("score_t + 1 is a plain offset, not a forward bar", False),
    ("Enter LONG at the open of the next bar if signal_t == 1", True),
    ("IF signal_t == TRUE AND close_t < open_t THEN enter LONG at open_{t+1}", True),
    ("Exit at the close of bar t+1 (one-bar hold)", True),
    ("Close position at the next bar's close following entry", True),
    ("Exit after h bars from entry or on trailing stop", True),
]

# Real lookahead that must be flagged, in every scope.
BAD = [
    ("r_event = (C_{t+e} - C_t) / C_t", False),
    ("persistence_signal = sign(r_{t+1}) * abs(r_lag)", False),
    ("signal_t = 1 if close_{t+1} > close_t else 0", False),
    ("use future returns to define regime", False),
    ("label = forward-looking 5-bar return", False),
    ("look-ahead window of 10 bars", False),
    ("Enter LONG if close_{t+1} > open_{t+1}", True),
    ("Exit if r_{t+1} < 0", True),
    ("Exit if the next bar close is below the stop", True),
    ("Enter when [t+3] high exceeds entry", True),
    ("Enter when t+2 close exceeds entry", True),
    # A fill phrase must not launder a forward condition on the same clause.
    ("Enter LONG if close_{t+1} > close_t, at the open of the next bar", True),
    ("Exit if close_{t+1} < stop", True),
    # Fills are not allowed inside a signal definition.
    ("signal_t = open_{t+1} - close_t", False),
    ("signal_t = 1 at the open of the next bar", False),
]


@pytest.mark.parametrize("text,allow_fill", GOOD)
def test_causal_specs_pass(text, allow_fill):
    assert find_lookahead(text, allow_fill=allow_fill) is None


@pytest.mark.parametrize("text,allow_fill", BAD)
def test_lookahead_specs_blocked(text, allow_fill):
    assert find_lookahead(text, allow_fill=allow_fill) is not None
