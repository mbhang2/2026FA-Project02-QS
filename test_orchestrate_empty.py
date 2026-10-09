import os
from Gowtam_hypothesis.researcher import llm, orchestrator

# Load the env file manually if needed
env_path = "Gowtam_hypothesis/.env"
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            if line.strip() and not line.startswith("#"):
                key, val = line.strip().split("=", 1)
                os.environ[key] = val.strip("'\"")

try:
    print("Testing orchestrate empty...")
    decision = orchestrator.orchestrate([])
    print("Decision:", decision)
except Exception as e:
    print("Orchestrate Error:", type(e).__name__, ":", e)
