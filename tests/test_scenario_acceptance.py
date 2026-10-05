from __future__ import annotations

import unittest
from pathlib import Path

from scenario_governance.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class ScenarioGovernanceAcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["old_decision_rewrite_blocked"])
        self.assertTrue(result["actual_rewrite_blocked"])
        self.assertFalse(result["variance_y1_beyond"])
        self.assertTrue(result["variance_y2_beyond"])
        self.assertEqual(result["active_plan"], "plan-revised")
        self.assertEqual(result["superseded"], result["first_decision"])
        self.assertGreaterEqual(result["objections_retained"], 1)
        self.assertTrue(result["audit_chain_valid"])
        self.assertEqual(result["schema"]["missing_tables"], [])
        self.assertNotEqual(result["snapshot_v1"], result["snapshot_v2"])


if __name__ == "__main__":
    unittest.main()
