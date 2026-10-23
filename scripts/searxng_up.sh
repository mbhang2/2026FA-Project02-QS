#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
docker rm -f searxng >/dev/null 2>&1 || true
docker run -d --name searxng -p 8080:8080 \
  -v "$REPO_ROOT/Gowtam_hypothesis/searxng:/etc/searxng" \
  searxng/searxng:latest
echo "SearXNG started on http://localhost:8080"
