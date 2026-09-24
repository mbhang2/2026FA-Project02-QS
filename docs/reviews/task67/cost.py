"""Task 67, cost half — what does each resolution cost through the real boundary?

Fidelity said 1s is closest to the tick profile. This says what each one costs
where it matters: a strategy in its own process, one pipe round-trip per
callback, against `nq-train-fitted`'s 7,200s budget over 384 sessions.
"""
from __future__ import annotations

import json, pathlib, sys, time
from dataclasses import dataclass

REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from src.core.engine import Engine                      # noqa: E402
from src.core.engine.remote import RemoteStrategy       # noqa: E402
from src.core.runlog import RunTicket                   # noqa: E402
from src.core.types import Split                        # noqa: E402

SESSIONS = 3
FULL_SPLIT = 384


@dataclass
class FirstSessions:
    """The dataset, truncated to its first `n` sessions of the split.

    Wraps rather than subclasses, the way `RotatedDataset` does, so the calendar,
    the split enforcement and the holdout lock behave exactly as they do for the
    real thing. Only `ticks` differs.
    """
    inner: object
    n: int

    @property
    def name(self) -> str:
        return f"{self.inner.name}:first={self.n}"

    def ticks(self, split, *, warmup: int = 0):
        seen, cur = 0, None
        for t in self.inner.ticks(split, warmup=warmup):
            if t.session != cur:
                cur = t.session
                seen += 1
                if seen > self.n:
                    return
            yield t

    def capabilities(self): return self.inner.capabilities()
    def calendar(self): return self.inner.calendar()
    def session_bounds(self, s): return self.inner.session_bounds(s)
    def digest(self): return self.inner.digest()
    def __getattr__(self, k): return getattr(self.inner, k)


PROBE = '''
class Strategy:
    """Subscribes and counts. No orders: this measures delivery, not trading."""
    def __init__(self):
        self.n = 0
    def setup(self, ctx):
        ctx.subscribe({sub!r})
    def on_bars(self, ctx):
        self.n += 1
    def on_tick(self, ctx):
        self.n += 1
    def on_fill(self, ctx): pass
    def on_lifecycle(self, ctx): pass
'''


def main() -> None:
    from src.core.data.nq import NQDataset
    scratch = pathlib.Path("/Users/minhyukbhang/.claude/jobs/d574cd40/tmp/task67")
    scratch.mkdir(parents=True, exist_ok=True)
    base = NQDataset()
    rows = []
    for sub in ("tick", "1s", "5s", "30s"):
        f = scratch / f"probe_{sub}.py"
        f.write_text(PROBE.format(sub=sub))
        ds = FirstSessions(base, SESSIONS)
        t0 = time.monotonic()
        with RemoteStrategy(f, "Strategy", timeout_s=None) as s:
            e = Engine(ds, s, ticket=RunTicket.for_null_calibration("task67 cost"),
                       split=Split.TRAIN, warmup_sessions=0,
                       fill_model="unfavorable", seed=1)
            e.run()
        dt = time.monotonic() - t0
        per_session = dt / SESSIONS
        full = per_session * FULL_SPLIT
        rows.append({"subscription": sub, "seconds": dt,
                     "per_session": per_session, "full_split_s": full,
                     "fits_7200s_budget": full < 7200})
        print(f"  {sub:5} {dt:7.1f}s over {SESSIONS} sessions "
              f"-> {per_session:6.1f}s/session "
              f"-> {full:8.0f}s for {FULL_SPLIT}  "
              f"{'FITS' if full < 7200 else 'OVER BUDGET'}")
    (pathlib.Path(__file__).parent / "cost.json").write_text(
        json.dumps({"sessions_measured": SESSIONS, "budget_s": 7200,
                    "rows": rows}, indent=2) + "\n")


if __name__ == "__main__":
    main()
