"""Finding 6's binary search, run against the static check.

Same attack as `docs/reviews/second-pass/attack_e.py`: write a strategy that
raises when its own realized P&L crosses a threshold, probe the free check, and
narrow the threshold from the exit code. Against the executing check it recovered
peak realized P&L to +/-0.0002 in 18 probes with 0 trials counted.

The exit code is now a property of the source text, so it cannot depend on P&L.
Proving that takes two searches rather than one: a single search still *appears*
to converge, because a constant answer drives the interval to one end and a
narrow interval looks like a result. So this runs the search against two
strategies whose realized P&L differs by construction -- one takes the long side
of every signal, the other the short side -- and compares.

If the channel carried any information the two searches would separate. They do
not: the same 18 answers, the same converged interval, on strategies whose actual
P&L is nowhere near each other.

    python docs/reviews/task23/oracle_after.py
"""
import json
import pathlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[3]
strat = REPO / "src" / "strategies" / "current.py"
orig = strat.read_text()
led = pathlib.Path(tempfile.mkdtemp()) / "l.jsonl"

TEMPLATE = '''
class Strategy:
    THRESHOLD = {threshold!r}
    def setup(self, ctx): ctx.subscribe("5m")
    def on_bars(self, ctx):
        if not ctx.actionable: return
        if not ctx.position and not ctx.working_order_ids:
            h = ctx.history("5m")
            if len(h) and h[-1].close > h[-1].open:
                ctx.{side}(1, stop_offset=0.75, target_offset=0.75)
    def on_fill(self, ctx):
        if ctx.realized_pnl > self.THRESHOLD:
            raise RuntimeError("oracle")
    def on_lifecycle(self, ctx): pass
'''


def probe(threshold, side):
    strat.write_text(TEMPLATE.format(threshold=threshold, side=side))
    r = subprocess.run([sys.executable, "scripts/agent_check.py",
                        "--ledger", str(led)],
                       capture_output=True, text=True, cwd=REPO)
    return r.returncode == 0


def search(side, probes=18):
    lo, hi, answers = -50.0, 50.0, []
    for _ in range(probes):
        mid = (lo + hi) / 2
        answer = probe(mid, side)
        answers.append(answer)
        if answer:
            hi = mid
        else:
            lo = mid
    return answers, (lo, hi)


def true_peak_pnl(side):
    """What the oracle was trying to learn, computed honestly for comparison."""
    sys.path.insert(0, str(REPO))
    from src.core.data.synthetic import SyntheticDataset, SyntheticSpec
    from src.core.engine import Engine
    from src.core.types import Split

    class S:
        def setup(self, ctx):
            ctx.subscribe("5m")

        def on_bars(self, ctx):
            if not ctx.actionable:
                return
            if not ctx.position and not ctx.working_order_ids:
                h = ctx.history("5m")
                if len(h) and h[-1].close > h[-1].open:
                    getattr(ctx, side)(1, stop_offset=0.75, target_offset=0.75)

        def on_fill(self, ctx):
            peak[0] = max(peak[0], ctx.realized_pnl)

        def on_lifecycle(self, ctx):
            pass

    peak = [float("-inf")]
    engine = Engine.for_test(
        SyntheticDataset(SyntheticSpec(n_sessions=40, ticks_per_session=390,
                                       vol_per_tick=0.15, seed=1)),
        S(), split=Split.VALIDATION, warmup_sessions=1, fill_model="unfavorable",
        seed=1,
    )
    engine.run()
    return peak[0]


try:
    long_answers, long_interval = search("buy")
    short_answers, short_interval = search("sell")

    print(f"  probes per search: {len(long_answers)}")
    print(f"  long  side: answers {sorted(set(long_answers))}  "
          f"interval [{long_interval[0]:+.4f}, {long_interval[1]:+.4f}]")
    print(f"  short side: answers {sorted(set(short_answers))}  "
          f"interval [{short_interval[0]:+.4f}, {short_interval[1]:+.4f}]")
    print(f"  the two searches are identical: "
          f"{long_answers == short_answers and long_interval == short_interval}")
    print()
    print(f"  what they were trying to learn, computed directly:")
    print(f"    long  peak realized P&L {true_peak_pnl('buy'):+.4f}")
    print(f"    short peak realized P&L {true_peak_pnl('sell'):+.4f}")
    print(f"  neither search moved toward either number, because the answer it "
          f"read was the same both times.")
    print()
    rows = [json.loads(x) for x in led.read_text().splitlines() if x.strip()]
    print(f"  ledger rows written: {len(rows)}  "
          f"purposes: {sorted({r['purpose'] for r in rows})}")
    print(f"  rows where strategy code ran: "
          f"{sum(1 for r in rows if r.get('executed_strategy_code'))}")
    print(f"  trials counted toward N: "
          f"{sum(1 for r in rows if r['purpose'] == 'trial')}")
finally:
    strat.write_text(orig)
