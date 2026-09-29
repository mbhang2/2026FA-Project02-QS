#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

MODE="${1:-all}"
if [ "$MODE" != "positive" ] && [ "$MODE" != "negative" ] && [ "$MODE" != "all" ]; then
    echo "Usage: $0 [positive|negative|all]"
    exit 1
fi

export ORACLE_EMBED_MODEL="BAAI/bge-m3"
export ORACLE_EMBED_BACKEND="sentence-transformers"
export ORACLE_EMBED_API_BASE=""

SOURCE_DOC=".aider_factory/markdown/lanceDB/microstructure/sample_microstructure.md"
if [ ! -f "$SOURCE_DOC" ]; then
    SOURCE_DOC=$(find .aider_factory/markdown/lanceDB/microstructure -maxdepth 1 -name "*.md" 2>/dev/null | head -n 1 || true)
fi

if [ -z "$SOURCE_DOC" ] || [ ! -f "$SOURCE_DOC" ]; then
    echo "Error: Extracted markdown source document not found in .aider_factory/markdown/lanceDB/microstructure."
    exit 1
fi
echo "Resolved source document: $SOURCE_DOC"

KB_DB=$(find .aider_factory/markdown/lanceDB/microstructure -name "lancedb" -type d 2>/dev/null | head -n 1 || true)
if [ -z "$KB_DB" ] || [ ! -d "$KB_DB" ]; then
    KB_DB=$(find .aider_factory -name "lancedb" -type d 2>/dev/null | head -n 1 || true)
fi
if [ -z "$KB_DB" ] || [ ! -d "$KB_DB" ]; then
    echo "Error: LanceDB vector database not found. Run 02_research_and_rag.sh first."
    exit 1
fi
echo "Resolved LanceDB directory: $KB_DB"

MINICHECK_ACTIVE=$(python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(0.5)
res = s.connect_ex(('127.0.0.1', 8090))
s.close()
print('1' if res == 0 else '0')
" 2>/dev/null || echo "0")

FAILED=0

run_validation_test() {
    local TEST_MODE="$1"
    local MEMO_SOURCE="$2"
    local REPORT_QUOTE="reports/quote_audit_${TEST_MODE}.md"
    local REPORT_CLAIMS="reports/claims_audit_${TEST_MODE}.md"

    echo "===================================================================="
    echo ">>> Running Validation in Mode: [${TEST_MODE^^}] <<<"
    echo "===================================================================="

    # Reset active memo to fresh state so autofix is deterministic across runs
    cp "$MEMO_SOURCE" docs/strategy_memo.md

    echo "=== Tier 1: Quote Validation & Autofix ==="
    set +e
    aider-validate --file docs/strategy_memo.md --source "$SOURCE_DOC" --collection microstructure --db "$KB_DB" --report "$REPORT_QUOTE" --autofix
    TIER1_EXIT=$?
    set -e

    if [ -f "$REPORT_QUOTE" ]; then
        echo "=== Quote Audit Report ($REPORT_QUOTE, Exit code: $TIER1_EXIT) ==="
        cat "$REPORT_QUOTE"
    fi

    echo "=== Tier 2: Claims Validation ==="
    set +e
    if [ "$MINICHECK_ACTIVE" = "1" ]; then
        echo "MiniCheck endpoint active on port 8090. Running NLI validation..."
        aider-validate --claims-only --file docs/strategy_memo.md --collection microstructure --db "$KB_DB" \
            --region-threshold 0.50 \
            --grounding-model "${GROUNDING_AGENT_MODEL:-openai/minicheck-flan-t5-large}" \
            --grounding-api-base "${GROUNDING_AGENT_API_BASE:-http://127.0.0.1:8090/v1}" \
            --grounding-api-key "sk-dummy" \
            --report "$REPORT_CLAIMS" --no-print
    else
        echo "MiniCheck endpoint inactive. Running vector cosine grounding check..."
        aider-validate --claims-only --file docs/strategy_memo.md --collection microstructure --db "$KB_DB" \
            --region-threshold 0.50 \
            --grounding-model "" \
            --report "$REPORT_CLAIMS" --no-print
    fi
    TIER2_EXIT=$?
    set -e

    if [ -f "$REPORT_CLAIMS" ]; then
        echo "=== Claims Audit Report ($REPORT_CLAIMS, Exit code: $TIER2_EXIT) ==="
        cat "$REPORT_CLAIMS"
    fi

    echo ""
    if [ "$TEST_MODE" = "positive" ]; then
        if [ $TIER1_EXIT -eq 0 ] && [ $TIER2_EXIT -eq 0 ]; then
            echo "✅ POSITIVE TEST RESULT: PASS (All quotes stitched/promoted, all claims grounded)"
        else
            echo "❌ POSITIVE TEST RESULT: UNEXPECTED FAILURE (Tier 1: $TIER1_EXIT, Tier 2: $TIER2_EXIT)"
            FAILED=1
        fi
    else
        if [ $TIER1_EXIT -ne 0 ] && [ $TIER2_EXIT -ne 0 ]; then
            echo "🛡️ NEGATIVE TEST RESULT: PASS (Both Tier 1 quote stitch and Tier 2 claims rejected ungrounded content)"
        else
            echo "❌ NEGATIVE TEST RESULT: FAILURE (Expected rejections: Tier 1: $TIER1_EXIT, Tier 2: $TIER2_EXIT)"
            FAILED=1
        fi
    fi
    echo ""
}

if [ "$MODE" = "positive" ] || [ "$MODE" = "all" ]; then
    run_validation_test "positive" "docs/strategy_memo_positive.md"
fi

if [ "$MODE" = "negative" ] || [ "$MODE" = "all" ]; then
    run_validation_test "negative" "docs/strategy_memo_negative.md"
fi

# Reset active memo back to positive state for post-run cleanliness
cp docs/strategy_memo_positive.md docs/strategy_memo.md

echo ""
echo "=== OpenCode TUI Equivalents ==="
echo "In OpenCode TUI, run:"
echo "  Tab -> select 'validator' -> aider-validate --file docs/strategy_memo.md --source \"$SOURCE_DOC\" --report reports/quote_audit.md --autofix"
echo "  Tab -> select 'validator' -> aider-validate --claims-only --file docs/strategy_memo.md --collection microstructure --report reports/claims_audit.md --no-print"

exit $FAILED
