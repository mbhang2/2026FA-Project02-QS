#!/usr/bin/env python3
"""Part A, first: find where the two platforms actually stop agreeing.

    python docs/reviews/task28/where_it_diverges.py > out.json

Task 26 reported the symptom -- `net_pnl` -48.86 against -48.85999999999994 --
and attributed it to numpy's pairwise summation. That was a guess. The blotter
digest matches across platforms, but `BlotterRow.as_row()` rounds to ten decimal
places before hashing, so "the digests match" is a much weaker statement than
"the rows are identical". This dumps everything at full precision so the diff
says where the agreement ends rather than where it was assumed to.

Emits JSON on stdout; run on both platforms and diff.
"""

from __future__ import annotations

import json
import math
import pathlib
import platform
import sys

REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from src.core.data.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
from src.core.engine import Engine  # noqa: E402
from src.core.scoring import metrics as metricsmod  # noqa: E402
from src.core.scoring.calibration import ReferenceStrategy  # noqa: E402
from src.core.scoring.scorer import score  # noqa: E402
from src.core.types import Split  # noqa: E402


def hexes(values) -> list[str]:
    """Full precision, unambiguously. `repr` of a float is shortest-round-trip,
    which is exact but easy to misread; the hex form is the bits."""
    return [float.hex(float(v)) for v in values]


def main() -> int:
    spec = SyntheticSpec(n_sessions=175, ticks_per_session=390,
                         vol_per_tick=0.15, seed=1)
    engine = Engine.for_test(SyntheticDataset(spec), ReferenceStrategy(),
                             split=Split.VALIDATION, warmup_sessions=1,
                             fill_model="unfavorable", seed=1)
    engine.run()
    blotter = engine.reconstructed()
    period = engine.session_calendar()
    result = score(blotter, period=period)

    rows = blotter.rows
    returns = metricsmod.session_returns(blotter, period)

    out = {
        "platform": platform.system(),
        "python": platform.python_version(),
        "numpy": __import__("numpy").__version__,
        "blotter_digest": blotter.digest(),
        "n_rows": len(rows),
        # the inputs, at full precision rather than rounded to ten places
        "row_prices": hexes(r.price for r in rows),
        "row_realized": hexes(r.realized_pnl for r in rows),
        "row_commission": hexes(r.commission for r in rows),
        # the first aggregation
        "sum_realized": float.hex(blotter.realized_pnl),
        "sum_commission": float.hex(blotter.commission),
        "fsum_realized": float.hex(math.fsum(r.realized_pnl for r in rows)),
        "fsum_commission": float.hex(math.fsum(r.commission for r in rows)),
        # per-session, which is what the metric consumes
        "n_returns": len(returns),
        "session_returns": hexes(returns),
        "sum_returns": float.hex(sum(returns)),
        "fsum_returns": float.hex(math.fsum(returns)),
        # and the score
        "score": {k: (float.hex(v) if isinstance(v, float) else v)
                  for k, v in result.as_dict().items()},
    }
    json.dump(out, sys.stdout, indent=1, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
