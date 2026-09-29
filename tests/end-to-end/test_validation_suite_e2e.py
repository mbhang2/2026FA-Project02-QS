import os
import re
import shutil
import subprocess
from pathlib import Path
import pytest


@pytest.fixture
def validation_env(tmp_path):
    """Initializes an isolated workspace with LanceDB config and live MiniCheck endpoint setup."""
    if not shutil.which("aider-oracle"):
        pytest.fail("aider-oracle is not installed or not in PATH.")
    if not shutil.which("aider-validate"):
        pytest.fail("aider-validate is not installed or not in PATH.")

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["PWD"] = str(workspace)

    # Clean ambient context files and prior session history
    env.pop("ORACLE_CONTEXT_FILES", None)
    env.pop("ORACLE_JOB_READ_FILES", None)
    env.pop("ORACLE_SESSION_FILE", None)
    env.pop("ORACLE_DEBATE_SESSION_FILE", None)

    # Embedding backend configuration
    env["ORACLE_EMBED_MODEL"] = "BAAI/bge-m3"
    env["ORACLE_EMBED_BACKEND"] = "sentence-transformers"

    # Live MiniCheck NLI grounding model configuration
    # MiniCheck is an unauthenticated local endpoint: scrub ambient secrets and use a dummy key
    env.pop("LITELLM_API_KEY", None)
    env.pop("GROUNDING_AGENT_API_KEY", None)
    env["GROUNDING_AGENT_MODEL"] = os.getenv(
        "GROUNDING_AGENT_MODEL", "openai/minicheck-flan-t5-large"
    )
    env["GROUNDING_AGENT_API_BASE"] = os.getenv(
        "GROUNDING_AGENT_API_BASE", "http://192.168.100.1:8090/v1"
    )
    env["GROUNDING_AGENT_API_KEY"] = "sk-dummy"
    env["OPENAI_API_BASE"] = env["GROUNDING_AGENT_API_BASE"]
    env["OPENAI_BASE_URL"] = env["GROUNDING_AGENT_API_BASE"]
    env["OPENAI_API_KEY"] = env["GROUNDING_AGENT_API_KEY"]

    # Workspace-local aider_factory configuration with batch mode enabled
    af_dir = workspace / ".aider_factory"
    af_dir.mkdir(parents=True, exist_ok=True)
    (af_dir / ".env.yml").write_text(
        f"working_directory: '{workspace}'\n"
        "rag:\n"
        "  embed_model: 'BAAI/bge-m3'\n"
        "  embed_backend: 'sentence-transformers'\n"
        "  batch: true\n",
        encoding="utf-8",
    )

    env["ORACLE_CONFIG_FILE"] = str(af_dir / ".env.yml")
    return workspace, env


def test_quote_validation_and_autofix_e2e(validation_env):
    """Tests exact quote extraction, ellipsis autofix repair, and negative tripwire."""
    workspace, env = validation_env

    source_file = workspace / "source_paper.md"
    source_file.write_text(
        "# High-Frequency Liquidity\n\n"
        "Empirical observations demonstrate that bid-ask spreads widen significantly in the "
        "first 15 minutes of trading due to inventory risk and information asymmetry.\n\n"
        "Market makers widen quoted spreads by an average of 2.4x the rolling baseline to "
        "protect against informed adverse selection before price discovery stabilizes.\n",
        encoding="utf-8",
    )

    # 1. Positive: Quote with ellipsis repaired via --autofix
    review_file = workspace / "review_memo.md"
    review_file.write_text(
        "# Review Memo\n\n"
        "The inventory dynamics are clear.\n\n"
        '[evidence] "bid-ask spreads widen significantly ... due to inventory risk"\n',
        encoding="utf-8",
    )
    report_file = workspace / "quote_report.md"

    res = subprocess.run(
        [
            "aider-validate",
            "--file",
            str(review_file),
            "--source",
            str(source_file),
            "--report",
            str(report_file),
            "--autofix",
        ],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"Quote autofix validation failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"

    updated_review = review_file.read_text(encoding="utf-8")
    assert "[fixed]" in updated_review, "Repaired quote was not relabeled to [fixed]"
    assert "..." not in updated_review, "Ellipsis was not repaired by --autofix"
    assert "in the first 15 minutes of trading due to inventory risk" in updated_review

    # 2. Negative: Fabricated quote absent from source fails closed
    trap_file = workspace / "trap_memo.md"
    trap_file.write_text(
        "# Trap Memo\n\n"
        '[evidence] "empirical backtesting proved an astronomical Sharpe ratio of 9.84 with zero drawdown"\n',
        encoding="utf-8",
    )
    trap_report = workspace / "trap_report.md"

    res_trap = subprocess.run(
        [
            "aider-validate",
            "--file",
            str(trap_file),
            "--source",
            str(source_file),
            "--report",
            str(trap_report),
        ],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
    )
    assert res_trap.returncode == 1, "Fabricated quote anchor should have returned exit code 1"
    assert trap_report.exists(), "Failure report was not created for fabricated quote anchor"
    trap_content = trap_report.read_text(encoding="utf-8")
    assert len(trap_content.strip()) > 0, "Trap report was unexpectedly empty"


def test_minicheck_nli_claims_validation_e2e(validation_env):
    """Tests Stage-2 MiniCheck NLI entailment scoring on continuous prose against LanceDB vector chunks."""
    workspace, env = validation_env

    source_file = workspace / "microstructure_paper.md"
    source_file.write_text(
        "# High-Frequency Liquidity and Spread Dynamics\n\n"
        "We examine the cross-section of liquid equity spreads during the opening auction "
        "and first 15 minutes of continuous trading.\n"
        "Empirical observations demonstrate that bid-ask spreads widen significantly in the "
        "first 15 minutes of trading due to inventory risk and information asymmetry.\n"
        "Market makers widen quoted spreads by an average of 2.4x the rolling baseline to "
        "protect against informed adverse selection before price discovery stabilizes.\n"
        "Consequently, liquidity-providing market makers earn a persistent premium when "
        "placing post-only limit orders at the wide spread, yielding an average reversion "
        "edge of 18 basis points over 5-minute intervals.\n",
        encoding="utf-8",
    )

    # Ingest document into LanceDB collection
    res_ingest = subprocess.run(
        [
            "aider-oracle",
            "--collection",
            "kb_minicheck",
            "--add-file",
            str(source_file),
        ],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
    )
    assert res_ingest.returncode == 0 and "Ingestion completed successfully" in res_ingest.stderr, (
        f"Knowledge base ingestion failed:\nSTDOUT:\n{res_ingest.stdout}\nSTDERR:\n{res_ingest.stderr}"
    )

    kb_db = workspace / ".aider_factory" / "markdown" / "lanceDB" / "kb_minicheck" / "lancedb"
    assert kb_db.exists(), f"LanceDB database missing at {kb_db}"

    model_name = env["GROUNDING_AGENT_MODEL"]

    # 1. Grounded Semantic Claim (MiniCheck Entailed -> Exit 0)
    grounded_claims = workspace / "grounded_claims.md"
    grounded_claims.write_text(
        "Market makers widen quoted spreads by an average of 2.4x the rolling baseline to protect against informed adverse selection before price discovery stabilizes.\n\n"
        "Liquidity-providing market makers earn a persistent premium when placing post-only limit orders at the wide spread, yielding an average reversion edge of 18 basis points over 5-minute intervals.\n",
        encoding="utf-8",
    )
    report_grounded = workspace / "report_grounded.md"

    res_grounded = subprocess.run(
        [
            "aider-validate",
            "--claims-only",
            "--file",
            str(grounded_claims),
            "--report",
            str(report_grounded),
            "--collection",
            "*",
            "--db",
            str(kb_db),
            "--region-threshold",
            "0.50",
            "--grounding-model",
            model_name,
            "--grounding-api-base",
            env["GROUNDING_AGENT_API_BASE"],
            "--grounding-api-key",
            env["GROUNDING_AGENT_API_KEY"],
            "--no-print",
        ],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
    )
    assert res_grounded.returncode == 0, (
        f"Grounded claims failed MiniCheck NLI validation:\nSTDOUT:\n{res_grounded.stdout}\nSTDERR:\n{res_grounded.stderr}"
    )

    # 2. Hallucinated / Contradictory Claim (MiniCheck Non-Entailed -> Exit 1)
    hallucinated_claims = workspace / "hallucinated_claims.md"
    hallucinated_claims.write_text(
        "Market makers tighten quoted spreads to zero in the first 15 minutes of trading to maximize volume turnover.\n\n"
        "Atmospheric spectroscopic observations of exoplanet HD 209458b confirm high concentrations of methane and water vapor.\n",
        encoding="utf-8",
    )
    report_hallucinated = workspace / "report_hallucinated.md"

    res_hallucinated = subprocess.run(
        [
            "aider-validate",
            "--claims-only",
            "--file",
            str(hallucinated_claims),
            "--report",
            str(report_hallucinated),
            "--collection",
            "*",
            "--db",
            str(kb_db),
            "--region-threshold",
            "0.50",
            "--grounding-model",
            model_name,
            "--grounding-api-base",
            env["GROUNDING_AGENT_API_BASE"],
            "--grounding-api-key",
            env["GROUNDING_AGENT_API_KEY"],
            "--no-print",
        ],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
    )
    assert res_hallucinated.returncode == 1, (
        "Hallucinated claims should have failed MiniCheck NLI validation with returncode 1"
    )
    assert report_hallucinated.exists(), "Report was not generated for hallucinated claims"
    report_text = report_hallucinated.read_text(encoding="utf-8")
    assert len(report_text.strip()) > 0, "Hallucinated report content was empty"


def test_full_validation_suite_hybrid_pipeline(validation_env):
    """Tests the complete hybrid pipeline: verbatim quote repairs followed by MiniCheck semantic checks."""
    workspace, env = validation_env

    source_file = workspace / "execution_theory.md"
    source_file.write_text(
        "# Market Microstructure and Order Execution\n\n"
        "Empirical observations demonstrate that bid-ask spreads widen significantly in the "
        "first 15 minutes of trading due to inventory risk and information asymmetry.\n\n"
        "Consequently, liquidity-providing market makers earn a persistent premium when "
        "placing post-only limit orders at the wide spread, yielding an average reversion "
        "edge of 18 basis points over 5-minute intervals.\n",
        encoding="utf-8",
    )

    # Step 1: Ingest into LanceDB
    res_ingest = subprocess.run(
        [
            "aider-oracle",
            "--collection",
            "kb_hybrid_test",
            "--add-file",
            str(source_file),
        ],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
    )
    assert res_ingest.returncode == 0

    kb_db = workspace / ".aider_factory" / "markdown" / "lanceDB" / "kb_hybrid_test" / "lancedb"
    assert kb_db.exists()

    # Step 2: Exact quote verification and autofix pass
    hybrid_doc = workspace / "strategy_memo.md"
    hybrid_doc.write_text(
        '[evidence] "bid-ask spreads widen significantly ... due to inventory risk"\n\n'
        "Consequently, liquidity-providing market makers earn a persistent premium when "
        "placing post-only limit orders at the wide spread, yielding an average reversion "
        "edge of 18 basis points over 5-minute intervals.\n",
        encoding="utf-8",
    )

    quote_report = workspace / "hybrid_quote_report.md"
    res_quote = subprocess.run(
        [
            "aider-validate",
            "--file",
            str(hybrid_doc),
            "--source",
            str(source_file),
            "--report",
            str(quote_report),
            "--autofix",
        ],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
    )
    assert res_quote.returncode == 0, f"Quote verification failed:\nSTDOUT:\n{res_quote.stdout}\nSTDERR:\n{res_quote.stderr}"
    assert "[fixed]" in hybrid_doc.read_text(encoding="utf-8")

    # Step 3: MiniCheck NLI check on semantic assertions
    nli_report = workspace / "hybrid_nli_report.md"
    res_nli = subprocess.run(
        [
            "aider-validate",
            "--claims-only",
            "--file",
            str(hybrid_doc),
            "--report",
            str(nli_report),
            "--collection",
            "*",
            "--db",
            str(kb_db),
            "--region-threshold",
            "0.50",
            "--grounding-model",
            env["GROUNDING_AGENT_MODEL"],
            "--grounding-api-base",
            env["GROUNDING_AGENT_API_BASE"],
            "--grounding-api-key",
            env["GROUNDING_AGENT_API_KEY"],
            "--no-print",
        ],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
    )
    assert res_nli.returncode == 0, f"MiniCheck NLI check failed on grounded hybrid document:\n{res_nli.stderr}"
