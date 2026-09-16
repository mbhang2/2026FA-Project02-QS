from __future__ import annotations

import unittest

from audit.types import GATE_ORDER, experiment_row, gate_summary, passed_audit


class TypesTests(unittest.TestCase):
    def test_gate_order_starts_with_reproduce_not_sharpe(self):
        self.assertEqual(GATE_ORDER[0], "reproduce")
        self.assertIn("stats", GATE_ORDER)

    def test_passed_audit_reads_first_row(self):
        packet = {
            "verdict": "FAIL",
            "experiments": [{"verdict": "PASS"}],
        }
        self.assertTrue(passed_audit(packet))

    def test_gate_summary(self):
        row = {"gates": {"leak": "PASS", "stats": "WEAK"}}
        summary = gate_summary(row)
        self.assertEqual(summary["leak"], "PASS")
        self.assertEqual(summary["stats"], "WEAK")
        self.assertEqual(experiment_row({"experiments": [row]}), row)


if __name__ == "__main__":
    unittest.main()
