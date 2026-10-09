import os
from Gowtam_hypothesis.researcher import llm

# Load the env file manually if needed
env_path = "Gowtam_hypothesis/.env"
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            if line.strip() and not line.startswith("#"):
                key, val = line.strip().split("=", 1)
                os.environ[key] = val.strip("'\"")

print("Using BASE_URL:", os.environ.get("LITELLM_BASE_URL", "https://llm-proxy.invalid/v1"))
print("Using API_KEY starts with:", (os.environ.get("LITELLM_API_KEY") or "")[:5])

try:
    print("Testing claude-haiku-4-5...")
    res = llm.preflight("claude-haiku-4-5")
    print("Preflight Success:", res)
except Exception as e:
    print("Preflight Error:", repr(e))

