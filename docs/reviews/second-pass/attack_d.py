"""D: the null tables are data, and the integrity check watches modules."""
import sys, os, json, shutil, pathlib, tempfile; sys.path.insert(0,".")
from src.core.scoring import integrity
from src.core.scoring.calibration import null as N

before = integrity.fingerprint()
real = pathlib.Path("data/calibration/empirical-null.json")
tmp = pathlib.Path(tempfile.mkdtemp())/"empirical-null.json"
shutil.copy(real, tmp)

# what a strategy would do: lower every quantile so anything clears
doc = json.loads(tmp.read_text())
for metric in doc["table"]:
    for n in doc["table"][metric]:
        q = doc["table"][metric][n]["quantiles"]
        for k in q:
            if q[k] is not None: q[k] = -9.99
tmp.write_text(json.dumps(doc))
N._CACHE_BY_PATH.clear()

print("  writable from strategy code:", os.access(real, os.W_OK))
print("  integrity fingerprint changed:", integrity.fingerprint() != before)
print("  threshold_for(0.001,40) from the doctored table:",
      N.threshold_for(0.001, 40, dataset="synthetic", path=tmp))
print("  far_for(-5.0, 40) from the doctored table:",
      N.far_for(-5.0, 40, dataset="synthetic", path=tmp))
N._CACHE_BY_PATH.clear()
print("\n  also writable:", {p: os.access(p, os.W_OK) for p in
      ("config/run.yaml", "config/calendars/nq-trades.yaml", "runs/ledger.jsonl",
       "data/nq-session-closes.json", "src/strategies/current.py")})
