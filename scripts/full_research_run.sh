#!/usr/bin/env bash
# One command: research ask -> hypothesis -> register -> run -> nulls -> report.
#
#   ./scripts/full_research_run.sh "gold mean reversion after extreme bars" --budget 1
#
# Loads the researcher's gateway key, lets optional RAG endpoints be pointed at
# local services when the caller has exported SEARXNG_BASE_URL / MINICHECK_API_BASE,
# enters the researcher workstream, and drives the full pipeline via --full.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RESEARCHER_DIR="$REPO_ROOT/Gowtam_hypothesis"
PY="$REPO_ROOT/.venv/bin/python"

# The gateway key lives in the workstream .env. Load it without clobbering a key
# the caller already exported.
if [ -f "$RESEARCHER_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$RESEARCHER_DIR/.env"
  set +a
fi

# Optional RAG endpoints: honored only if the caller did not already set them.
# SearXNG is the local metasearch backend; MiniCheck is the NLI grounding. Both
# degrade to a no-op when unset, so they are optional by design.
export SEARXNG_BASE_URL="${SEARXNG_BASE_URL:-}"
export MINICHECK_API_BASE="${MINICHECK_API_BASE:-}"

cd "$RESEARCHER_DIR"
exec "$PY" -m researcher "$@" --full