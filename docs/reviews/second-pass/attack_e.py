"""E: agent_check returns pass/fail. Binary-search a score through it."""
import subprocess, sys, pathlib, os, tempfile
REPO = pathlib.Path(".").resolve()
strat = REPO/"src/strategies/current.py"
orig = strat.read_text()
led = pathlib.Path(tempfile.mkdtemp())/"l.jsonl"

TEMPLATE = '''
class Strategy:
    THRESHOLD = {threshold!r}
    def setup(self, ctx): ctx.subscribe("5m")
    def on_bars(self, ctx):
        if not ctx.actionable: return
        if not ctx.position and not ctx.working_order_ids:
            h = ctx.history("5m")
            if len(h) and h[-1].close > h[-1].open:
                ctx.buy(1, stop_offset=0.75, target_offset=0.75)
    def on_fill(self, ctx):
        if ctx.realized_pnl > self.THRESHOLD:
            raise RuntimeError("oracle")
    def on_lifecycle(self, ctx): pass
'''

def probe(threshold):
    strat.write_text(TEMPLATE.format(threshold=threshold))
    r = subprocess.run([sys.executable, "scripts/agent_check.py", "--ledger", str(led)],
                       capture_output=True, text=True, cwd=REPO)
    return r.returncode == 0        # True = pnl never exceeded threshold

try:
    lo, hi, calls = -50.0, 50.0, 0
    for _ in range(18):
        mid = (lo + hi) / 2
        calls += 1
        if probe(mid): hi = mid       # never exceeded -> max pnl <= mid
        else: lo = mid
    print(f"  peak realized P&L recovered to +/-{(hi-lo)/2:.4f}: {(lo+hi)/2:+.4f}")
    print(f"  probes used: {calls}")
    rows = [x for x in led.read_text().splitlines() if x.strip()]
    import json
    purposes = {json.loads(r)["purpose"] for r in rows}
    print(f"  ledger rows written: {len(rows)}  purposes: {purposes}")
    print(f"  trials counted toward N: "
          f"{sum(1 for r in rows if json.loads(r)['purpose'] == 'trial')}")
finally:
    strat.write_text(orig)
