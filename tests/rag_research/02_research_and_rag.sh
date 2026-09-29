#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

export ORACLE_EMBED_MODEL="BAAI/bge-m3"
export ORACLE_EMBED_BACKEND="sentence-transformers"
export ORACLE_EMBED_API_BASE=""

echo "=== Step 1: Web Discovery via SearXNG ==="
aider-research search "high frequency market microstructure liquidity" --academic --top 3 --links-only --out temp/arxiv_urls.txt || true

if [ -s "temp/arxiv_urls.txt" ]; then
    echo "Harvested URLs:"
    cat temp/arxiv_urls.txt
fi

echo "=== Step 2: Remote Web Ingestion ==="
if [ -s "temp/arxiv_urls.txt" ]; then
    SAMPLE_URL=$(head -n 1 temp/arxiv_urls.txt)
    echo "Attempting web fetch for $SAMPLE_URL..."
    aider-oracle --collection microstructure --add-web "$SAMPLE_URL" || echo "WARNING: Web fetch failed or timed out. Falling back to local PDF."
else
    echo "No harvested URLs found. Skipping web fetch."
fi

echo "=== Step 3: Local PDF Ingestion via Docling ==="
aider-oracle --collection microstructure --add-file samples/sample_microstructure.pdf

echo "=== Step 4: Database Verification ==="
aider-oracle --collection microstructure --list
aider-oracle --collection microstructure --list-files

echo "=== Step 5: Semantic RAG Query ==="
aider-oracle --collection microstructure "What was the year-over-year CPI inflation rate through December 2025?"

echo ""
echo "=== OpenCode TUI Equivalents ==="
echo "In OpenCode TUI, run:"
echo "  Tab -> select 'researcher' -> aider-research search \"high frequency market microstructure liquidity\" --academic --top 3 --out temp/research.md"
echo "  Tab -> select 'oracle' -> aider-oracle --collection microstructure \"What was the year-over-year CPI inflation rate through December 2025?\""
