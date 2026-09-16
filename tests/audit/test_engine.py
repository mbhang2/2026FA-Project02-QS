from __future__ import annotations

import unittest

from audit.config import REPO_ROOT
from audit.engine import audit_experiment, audit_ticket_dir, load_strategy
from audit.engine.protocol import load_experiment


class EngineTests(unittest.TestCase):
    def test_strategy_alias_from_experiment_json(self):
        path = REPO_ROOT / "research/examples/buy-hold-sanity/experiments/00-zero-cost.json"
        exp = load_experiment(path)
        strategy = load_strategy(exp["strategy"])
        self.assertEqual(strategy.name, "buy_hold")

    def test_cheat_audit_fails(self):
        path = REPO_ROOT / "research/examples/buy-hold-sanity/experiments/01-lookahead-cheat.json"
        result = audit_experiment(path, seed=7)
        self.assertEqual(result["verdict"], "FAIL")
        self.assertEqual(result["leak"]["verdict"], "FAIL")
        self.assertEqual(result["gates"]["leak"], "FAIL")
        self.assertGreater(result["stats"]["psr"], 0.95)

    def test_buy_hold_audit_passes(self):
        path = REPO_ROOT / "research/examples/buy-hold-sanity/experiments/00-zero-cost.json"
        result = audit_experiment(path, seed=7)
        self.assertEqual(result["verdict"], "PASS")
        self.assertTrue(result["pit_ok"])
        self.assertEqual(result["stats"]["stats_gate"], "SKIP")

    def test_metric_mismatch_fails_reproduce(self):
        path = REPO_ROOT / "research/examples/buy-hold-sanity/experiments/00-zero-cost.json"
        result = audit_experiment(
            path,
            seed=7,
            previous={
                "metrics": {
                    "n_bars": 1305,
                    "sharpe": 99.0,
                    "total_return": 0.0,
                    "max_drawdown": 0.0,
                    "turnover": 0.0,
                }
            },
        )
        self.assertEqual(result["verdict"], "FAIL")
        self.assertEqual(result["gates"]["reproduce"], "FAIL")

    def test_tsmom_ticket_pause(self):
        ticket = REPO_ROOT / "research/examples/tsmom-pause"
        packet = audit_ticket_dir(ticket, seed=7)
        realistic = next(r for r in packet["experiments"] if r.get("experiment_id") == "01-realistic")
        self.assertEqual(realistic["verdict"], "PASS")
        self.assertEqual(realistic["action"], "pause")


if __name__ == "__main__":
    unittest.main()
