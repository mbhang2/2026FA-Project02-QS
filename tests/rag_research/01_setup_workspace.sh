#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

for bin in aider-helper aider-oracle aider-research aider-validate; do
    if ! command -v "$bin" >/dev/null 2>&1; then
        echo "Error: $bin is not installed or not in PATH."
        echo "Please install it using: uv tool install --force git+https://github.com/bryan506/aider-factory.git"
        exit 1
    fi
done

mkdir -p temp reports docs samples

for f in "samples/sample_microstructure.pdf" "docs/strategy_memo_positive.md" "docs/strategy_memo_negative.md"; do
    if [ ! -f "$f" ]; then
        echo "Error: $f does not exist."
        exit 1
    fi
done

if [ ! -f "docs/strategy_memo.md" ]; then
    cp docs/strategy_memo_positive.md docs/strategy_memo.md
fi

if [ -z "${GEMINI_API_KEY:-}" ] && [ -z "${OPENAI_API_KEY:-}" ] && [ -z "${LITELLM_API_KEY:-}" ]; then
    export OPENAI_API_KEY="sk-dummy"
fi

export ORACLE_EMBED_MODEL="BAAI/bge-m3"
export ORACLE_EMBED_BACKEND="sentence-transformers"
export ORACLE_EMBED_API_BASE=""

aider-helper bootstrap

# Ensure .aider_factory/.env.yml exists for standard CLI auto-discovery
REPO_NAME="$(basename "$SCRIPT_DIR" | tr ' ' '_')"
if [ -f ".aider_factory/.env_${REPO_NAME}.yml" ] && [ ! -f ".aider_factory/.env.yml" ]; then
    cp ".aider_factory/.env_${REPO_NAME}.yml" ".aider_factory/.env.yml"
    echo "Linked .aider_factory/.env_${REPO_NAME}.yml -> .aider_factory/.env.yml"
elif [ ! -f ".aider_factory/.env.yml" ]; then
    GENERATED_CONFIG=$(find .aider_factory -maxdepth 1 -name ".env_*.yml" 2>/dev/null | head -n 1 || true)
    if [ -n "$GENERATED_CONFIG" ]; then
        cp "$GENERATED_CONFIG" ".aider_factory/.env.yml"
        echo "Linked $GENERATED_CONFIG -> .aider_factory/.env.yml"
    fi
fi

# Sanitize any scaffolded placeholder router host from all env configs
python3 -c "import glob; [open(p, 'w').write(open(p).read().replace('http://<your-router-host>:4000/v1', '')) for p in glob.glob('.aider_factory/.env*.yml')]" 2>/dev/null || true

chmod +x 01_setup_workspace.sh 02_research_and_rag.sh 03_draft_and_validate.sh
