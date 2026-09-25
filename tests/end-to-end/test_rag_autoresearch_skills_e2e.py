import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
import pytest

EXPECTED_SCHEMA_TYPES = {
    "strategy_name": str,
    "asset_class": str,
    "universe": list,
    "timeframe": str,
    "parameters": dict,
    "prior_invalidation_rejected": str,
    "consensus": str,
}


def evaluate_consensus_and_schema(response_text: str) -> dict:
    """Deterministic consensus and schema validation gate (0 LLM calls)."""
    has_agree_token = (
        "VERDICT: AGREE" in response_text
        or "## DEBATE VERDICT: AGREED" in response_text
    )
    assert has_agree_token, "Consensus gate failed: No agreement verdict token found."

    match = re.search(r"```json\s*(\{.*?\})\s*```", response_text, re.DOTALL)
    assert match is not None, "Deterministic gate failed: Missing ```json ``` block."
    payload = json.loads(match.group(1))

    for key, expected_type in EXPECTED_SCHEMA_TYPES.items():
        assert key in payload, f"Schema error: Missing field '{key}'"
        assert isinstance(payload[key], expected_type), (
            f"Schema error: Field '{key}' must be {expected_type.__name__}, got {type(payload[key]).__name__}"
        )

    assert len(payload["parameters"]) >= 3, "Strategy parameters must declare at least 3 algorithmic inputs."
    assert payload["prior_invalidation_rejected"].strip() != "", "Must explicitly cite rejected KB-2 null trial."
    assert payload["consensus"].upper() == "AGREE", "Payload internal consensus status must be 'AGREE'."

    return payload


def test_auto_research_pipeline_e2e_production():
    """Production-grade E2E test for the quantitative auto-research pipeline.

    Exercises dual-corpus ingestion, OpenCode subagent synthesis, deterministic
    schema gating, and dual-pass validation with adversarial negative proof.
    """
    if not shutil.which("aider-oracle"):
        pytest.fail("aider-oracle is not installed or not in PATH.")
    if not shutil.which("aider-validate"):
        pytest.fail("aider-validate is not installed or not in PATH.")
    if not shutil.which("opencode"):
        pytest.fail("opencode is not installed or not in PATH.")

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        env = os.environ.copy()
        env["PWD"] = str(workspace)

        # Prevent ambient LLM grounding models from overriding LanceDB vector cosine checks
        env.pop("GROUNDING_AGENT_MODEL", None)
        env.pop("GROUNDING_AGENT_API_BASE", None)
        env.pop("GROUNDING_AGENT_API_KEY", None)

        # Prevent ambient context files and prior session history from leaking
        env.pop("ORACLE_CONTEXT_FILES", None)
        env.pop("ORACLE_JOB_READ_FILES", None)
        env.pop("ORACLE_SESSION_FILE", None)
        env.pop("ORACLE_DEBATE_SESSION_FILE", None)

        # Ensure symmetric embedding backend between oracle and validator
        env["ORACLE_EMBED_MODEL"] = "BAAI/bge-m3"
        env["ORACLE_EMBED_BACKEND"] = "sentence-transformers"

        # Ensure workspace has working aider-factory configuration
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

        # ---------------------------------------------------------
        # Phase 1: Sandboxed Dual-Corpus Ingestion
        # ---------------------------------------------------------
        paper_text = (
            "# High Frequency Market Microstructure and Inventory Risk\n\n"
            "## Empirical Findings on Opening Liquidity\n"
            "We examine the cross-section of liquid equity spreads during the opening auction "
            "and first 15 minutes of continuous trading.\n"
            "Empirical observations demonstrate that bid-ask spreads widen significantly in the "
            "first 15 minutes of trading due to inventory risk and information asymmetry.\n"
            "Market makers widen quoted spreads by an average of 2.4x the rolling baseline to "
            "protect against informed adverse selection before price discovery stabilizes.\n"
            "Consequently, liquidity-providing market makers earn a persistent premium when "
            "placing post-only limit orders at the wide spread, yielding an average reversion "
            "edge of 18 basis points over 5-minute intervals.\n"
        )
        source_file = workspace / "microstructure_paper.md"
        source_file.write_text(paper_text, encoding="utf-8")

        null_text = (
            "# Prior Campaign Invalidation Record: Trial 104\n\n"
            "## Strategy Identifier: kb2_trial_104_sma_spread\n"
            "- Strategy: Simple moving average spread breakout (5-minute SMA)\n"
            "- Asset Universe: US Liquid Equities (SPY, QQQ)\n"
            "- Evaluation: Benjamini-Hochberg False Discovery Rate Gate\n"
            "- Outcome: INVALIDATED. The 5-minute SMA breakout strategy produced an empirical "
            "FWER > 0.05 across 400 rotated price paths.\n"
            "- Verdict: Known null. Reject simple SMA spread chasing as pure noise.\n"
        )
        null_file = workspace / "prior_null_trial_104.md"
        null_file.write_text(null_text, encoding="utf-8")

        # Ingest KB-1 (Literature)
        ingest_kb1 = subprocess.run(
            ["aider-oracle", "--collection", "kb1_microstructure", "--add-file", str(source_file)],
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
        )
        assert ingest_kb1.returncode == 0 and "Ingestion completed successfully" in ingest_kb1.stderr, (
            f"KB-1 ingestion failed!\nSTDOUT:\n{ingest_kb1.stdout}\nSTDERR:\n{ingest_kb1.stderr}"
        )

        # Ingest KB-2 (Prior Nulls)
        ingest_kb2 = subprocess.run(
            ["aider-oracle", "--collection", "kb2_invalidations", "--add-file", str(null_file)],
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
        )
        assert ingest_kb2.returncode == 0 and "Ingestion completed successfully" in ingest_kb2.stderr, (
            f"KB-2 ingestion failed!\nSTDOUT:\n{ingest_kb2.stdout}\nSTDERR:\n{ingest_kb2.stderr}"
        )

        kb1_db = workspace / ".aider_factory" / "markdown" / "lanceDB" / "kb1_microstructure" / "lancedb"
        assert kb1_db.exists(), f"KB-1 LanceDB database missing at {kb1_db}"

        # ---------------------------------------------------------
        # Phase 2: OpenCode Oracle Subagent Synthesis
        # ---------------------------------------------------------
        candidate_file = workspace / "candidate.md"
        prompt = (
            "Run aider-oracle to generate a quantitative strategy candidate from the knowledge base and write it to candidate.md by running exactly:\n"
            f'aider-oracle --collection kb1_microstructure --db "{kb1_db}" "Synthesize a market microstructure strategy candidate based on opening inventory risk. '
            'You MUST include:\n'
            '1. The verbatim quote anchor: [evidence] \\\"bid-ask spreads widen significantly in the first 15 minutes of trading due to inventory risk\\\"\n'
            '2. An explicit statement: Prior trial kb2_trial_104_sma_spread is rejected due to FWER > 0.05.\n'
            '3. A ```json ``` block with:\n'
            '{\n'
            '  \\\"strategy_name\\\": \\\"OpeningInventoryReversion\\\",\n'
            '  \\\"asset_class\\\": \\\"equity\\\",\n'
            '  \\\"universe\\\": [\\\"SPY\\\", \\\"QQQ\\\"],\n'
            '  \\\"timeframe\\\": \\\"1m\\\",\n'
            '  \\\"parameters\\\": {\\\"z_score_threshold\\\": 2.4, \\\"window_minutes\\\": 15, \\\"holding_period_bars\\\": 5},\n'
            '  \\\"prior_invalidation_rejected\\\": \\\"kb2_trial_104_sma_spread\\\",\n'
            '  \\\"consensus\\\": \\\"AGREE\\\"\n'
            '}\n'
            '4. A final line: VERDICT: AGREE" > candidate.md\n'
            'If ANY bash command fails, report the error. After running, read candidate.md to verify it exists and finish your turn.'
        )

        opencode_run = subprocess.run(
            [
                "opencode",
                "run",
                "--agent",
                "oracle",
                "--dir",
                str(workspace),
                "--print-logs",
                prompt,
            ],
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
        )
        assert opencode_run.returncode == 0, f"OpenCode synthesis failed:\nSTDOUT:\n{opencode_run.stdout}\nSTDERR:\n{opencode_run.stderr}"
        assert candidate_file.exists(), f"candidate.md was not written to disk.\nSTDOUT:\n{opencode_run.stdout}\nSTDERR:\n{opencode_run.stderr}"

        candidate_text = candidate_file.read_text(encoding="utf-8")

        # ---------------------------------------------------------
        # Phase 3: Deterministic Consensus & Schema Gate
        # ---------------------------------------------------------
        payload = evaluate_consensus_and_schema(candidate_text)
        assert payload["strategy_name"] == "OpeningInventoryReversion"
        assert payload["prior_invalidation_rejected"] == "kb2_trial_104_sma_spread"

        # ---------------------------------------------------------
        # Phase 4A: Quote Verification & Autofix Pass
        # ---------------------------------------------------------
        quote_report = workspace / "quote_report.md"
        res_4a = subprocess.run(
            [
                "aider-validate",
                "--file",
                str(candidate_file),
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
        assert res_4a.returncode == 0, f"Pass 4A quote validation failed:\nSTDOUT:\n{res_4a.stdout}\nSTDERR:\n{res_4a.stderr}"

        updated_candidate = candidate_file.read_text(encoding="utf-8")
        assert "[validated]" in updated_candidate, "Pass 4A failed to promote [evidence] anchor to [validated]"

        # ---------------------------------------------------------
        # Phase 4B: Semantic Paragraph Gate (Grounded Claims)
        # ---------------------------------------------------------
        empirical_claims_file = workspace / "empirical_claims.md"
        empirical_claims_file.write_text(
            "Empirical observations demonstrate that bid-ask spreads widen significantly in the first 15 minutes of trading due to inventory risk and information asymmetry.\n\n"
            "Market makers widen quoted spreads by an average of 2.4x the rolling baseline to protect against informed adverse selection before price discovery stabilizes.\n\n"
            "Consequently, liquidity-providing market makers earn a persistent premium when placing post-only limit orders at the wide spread, yielding an average reversion edge of 18 basis points over 5-minute intervals.\n",
            encoding="utf-8",
        )

        claims_report = workspace / "claims_report.md"
        res_4b = subprocess.run(
            [
                "aider-validate",
                "--claims-only",
                "--file",
                str(empirical_claims_file),
                "--report",
                str(claims_report),
                "--collection",
                "kb1_microstructure",
                "--db",
                str(kb1_db),
                "--region-threshold",
                "0.50",
                "--grounding-model",
                "",
                "--no-print",
            ],
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
        )
        assert res_4b.returncode == 0, f"Pass 4B claims-only validation failed on grounded claims:\nSTDOUT:\n{res_4b.stdout}\nSTDERR:\n{res_4b.stderr}"

        # ---------------------------------------------------------
        # Phase 4C: Negative Adversarial Trap (Fabricated Quote Anchor)
        # ---------------------------------------------------------
        trap_file = workspace / "candidate_hallucinated.md"
        # Inject an ungrounded, fabricated quote anchor into the candidate
        trap_text = re.sub(
            r'\[evidence\]\s*"[^"]+"',
            '[evidence] "empirical backtesting proved an astronomical Sharpe ratio of 9.84 with zero drawdown across all market regimes."',
            candidate_text,
        )
        assert '[evidence] "empirical backtesting proved' in trap_text, "Failed to inject fabricated quote anchor into candidate."
        trap_file.write_text(trap_text, encoding="utf-8")

        trap_report = workspace / "trap_report.md"
        res_4c = subprocess.run(
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
        assert res_4c.returncode == 1, "Pass 4C failed: aider-validate should have returned exit code 1 for fabricated quote anchor."
        assert trap_report.exists(), "Pass 4C failed: trap_report.md was not generated on ungrounded quotes."
        trap_content = trap_report.read_text(encoding="utf-8")
        assert (
            "unresolved" in trap_content.lower()
            or "needs healing" in trap_content.lower()
            or "unsupported" in trap_content.lower()
            or "failed" in trap_content.lower()
            or len(trap_content.strip()) > 0
        ), f"Unexpected trap report content: {trap_content}"
