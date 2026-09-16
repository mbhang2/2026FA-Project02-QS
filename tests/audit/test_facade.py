from __future__ import annotations

import unittest
from pathlib import Path

from audit.config import REPO_ROOT
from audit.facade import format_brief, list_experiments, resolve_id, run_audit
from audit.types import passed_audit


class FacadeTests(unittest.TestCase):
    def test_resolve_aliases(self):
        self.assertEqual(resolve_id("kill"), "01-lookahead-cheat")
        self.assertEqual(resolve_id("pause"), "01-realistic")
        self.assertEqual(resolve_id("nvda-1m-realistic"), "nvda-1m-realistic")

    def test_list_includes_demo_ids(self):
        ids = {r["experiment_id"] for r in list_experiments(root=REPO_ROOT)}
        self.assertIn("01-lookahead-cheat", ids)
        self.assertIn("01-realistic", ids)

    def test_cheat_audit_kills_without_store(self):
        packet = run_audit("01-lookahead-cheat", store=False)
        row = packet["experiments"][0]
        self.assertEqual(row["verdict"], "FAIL")
        self.assertEqual(row["action"], "kill")
        self.assertFalse(passed_audit(packet))
        self.assertIn("VERDICT: FAIL", format_brief(packet))

    def test_list_includes_nvda_demo(self):
        ids = {r["experiment_id"] for r in list_experiments(root=REPO_ROOT)}
        self.assertIn("nvda-1m-realistic", ids)


if __name__ == "__main__":
    unittest.main()
