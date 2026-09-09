"""The calibration rig: immutable apparatus, not part of the harness under test.

**`src.strategies` may not import this.** The reference strategy lives here for
the same reason the scorer does — a power curve measured with the agent's file in
the loop measures the metric, the threshold *and* whatever that file happens to
contain, with no way to separate them.

What is here:

* `reference` — the strategy the rig measures with. Volatility-scaled, so its
  payoff shape is invariant to signal strength.
* `null` — the empirical null distribution of the score under no signal, and the
  `threshold_for` / `far_for` pair that turns it into numbers an accept rule can
  use.
* `_sweep` — running trials and sweeping planted-signal strength.

Nothing here decides accept policy. It supplies the numbers accept policy needs.
"""

from __future__ import annotations

import inspect

_FORBIDDEN_PREFIX = "src.strategies"


def _reject_import_from_strategies() -> None:
    for frame in inspect.stack():
        name = frame.frame.f_globals.get("__name__", "")
        if name == _FORBIDDEN_PREFIX or name.startswith(_FORBIDDEN_PREFIX + "."):
            raise ImportError(
                f"{name} may not import src.core.calibration. The rig measures the "
                "harness; a strategy that can reach it can be written against the "
                "null it is being judged against."
            )


_reject_import_from_strategies()

from ._sweep import (  # noqa: E402
    AcceptRule,
    EmpiricalAcceptRule,
    TrialResult,
    acceptance_rate,
    run_trial,
)
from .reference import ReferenceStrategy  # noqa: E402

__all__ = [
    "AcceptRule",
    "EmpiricalAcceptRule",
    "ReferenceStrategy",
    "TrialResult",
    "acceptance_rate",
    "run_trial",
]
