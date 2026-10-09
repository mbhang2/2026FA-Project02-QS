import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Set PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Load environment variables
load_dotenv("Gowtam_hypothesis/.env")

from Gowtam_hypothesis.researcher import driver

print("Running hypothesis generation...")
result = driver.run("Is there a statistical edge in gold futures (XAUUSD) intraday momentum?", budget=1)

print(f"\n--- Run complete ---")
print(f"Emitted: {result.n_emitted}")
print(f"Blocked: {result.n_blocked}")

for r in result.emitted:
    path = Path("config/hypotheses") / f"{r.id}.md"
    print(f"\n--- Output File: {path} ---")
    print(path.read_text())

print(f"\n--- Ledger Output ---")
ledger_path = Path("config/hypotheses/ledger.jsonl")
if ledger_path.exists():
    with open(ledger_path, "r") as f:
        lines = f.readlines()
        print("".join(lines[-3:]))
