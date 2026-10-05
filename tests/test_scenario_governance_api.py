from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scenario_governance.acceptance import plan_payload, snapshot_payload
from scenario_governance.api import JsonApplication
from scenario_governance.clock import FrozenClock
from scenario_governance.service import GovernanceService
from scenario_governance.storage import connect


class GovernanceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = connect(":memory:")
        self.service = GovernanceService(
            self.connection, FrozenClock(datetime(2026, 10, 5, tzinfo=timezone.utc)))
        self.app = JsonApplication(self.service)
        for uid, name, role in (
            ("res-1", "油藏", "reservoir"), ("fac-1", "工程", "facility"),
            ("fin-1", "财务", "finance"), ("dm-1", "决策", "decision_maker"),
            ("aud-1", "审计", "auditor")):
            self.service.create_user(uid, name, role)
        self.headers = {"X-Actor-Id": "res-1"}

    def tearDown(self) -> None:
        self.connection.close()

    def _post(self, path: str, payload: dict, actor: str = "res-1"):
        return self.app.handle(
            "POST", path, {"X-Actor-Id": actor},
            json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _seed_approved_plan(self, plan_id: str = "plan-balanced") -> str:
        response = self._post("/snapshots", snapshot_payload())
        digest = response.body["content_sha256"]
        self._post("/plans", plan_payload(
            plan_id, digest, name="稳健方案",
            groups=["wg-north-ridge", "wg-south-flank"],
            capex_items=["capex-drilling-north", "capex-drilling-south", "capex-fpso-retrofit"],
            production=["60", "90", "88", "76", "62"]))
        evaluate = self.app.handle(
            "POST", f"/plans/{plan_id}/evaluate", {"X-Actor-Id": "fin-1"})
        self.assertEqual(evaluate.status, 200)
        decision = self._post("/decisions", {
            "plan_id": plan_id, "outcome": "approved", "conclusion": "同意",
            "expected_revision": 1,
            "objections": [{"raised_by": "fac-1", "content": "窗口紧张"}]},
            actor="dm-1")
        self.assertEqual(decision.status, 201)
        return digest

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)

    def test_actor_header_required(self) -> None:
        response = self.app.handle(
            "POST", "/snapshots", {},
            json.dumps(snapshot_payload()).encode("utf-8"))
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_full_decision_trace_route(self) -> None:
        digest = self._seed_approved_plan()
        response = self.app.handle(
            "POST", "/plans/plan-balanced/dossier", {"X-Actor-Id": "aud-1"})
        self.assertEqual(response.status, 200)
        body = response.body
        self.assertEqual(body["snapshot"]["content_sha256"], digest)
        self.assertEqual(body["decisions"][0]["outcome"], "approved")
        self.assertEqual(len(body["decisions"][0]["objections"]), 1)
        self.assertIn("sensitivity", body["plan"]["evaluation"])

    def test_compare_route(self) -> None:
        digest = self._seed_approved_plan()
        self._post("/plans", plan_payload(
            "plan-aggressive", digest, name="激进方案",
            groups=["wg-north-ridge"],
            capex_items=["capex-drilling-north", "capex-fpso-retrofit"],
            production=["42", "56", "48", "40", "32"]))
        self.app.handle("POST", "/plans/plan-aggressive/evaluate", {"X-Actor-Id": "fin-1"})
        response = self._post(
            "/plans/compare",
            {"plan_ids": ["plan-balanced", "plan-aggressive"]}, actor="dm-1")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["count"], 2)
        self.assertEqual(
            response.body["ranking_by_base_npv"][0], "plan-balanced")

    def test_actuals_route_blocks_rewrite(self) -> None:
        self._seed_approved_plan()
        payload = {
            "plan_id": "plan-balanced", "period_year": 1,
            "production_10kt": "60.5", "water_cut_percent": "80.2",
            "downtime_days": "0", "capex_spent_wan_yuan": "162000",
            "price_usd_bbl": "74.5"}
        first = self._post("/actuals", payload, actor="fac-1")
        self.assertEqual(first.status, 201)
        second = self._post("/actuals", payload, actor="fac-1")
        self.assertEqual(second.status, 409)

    def test_active_decision_route(self) -> None:
        self._seed_approved_plan()
        response = self.app.handle(
            "GET", "/assets/liuhua-deepwater/decision", {"X-Actor-Id": "aud-1"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["decision"]["plan_id"], "plan-balanced")

    def test_audit_chain_route(self) -> None:
        self._seed_approved_plan()
        response = self.app.handle("GET", "/audit/chain", {"X-Actor-Id": "aud-1"})
        self.assertEqual(response.status, 200)
        self.assertTrue(response.body["valid"])


class ConcurrentApprovalTests(unittest.TestCase):
    """并发批准同一油田的不同方案时，只能有一个生效修订。"""

    def test_only_one_approval_survives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "concurrent.sqlite3"
            setup_connection = connect(database)
            setup = GovernanceService(setup_connection)
            for uid, name, role in (
                ("res-1", "油藏", "reservoir"), ("fac-1", "工程", "facility"),
                ("fin-1", "财务", "finance"), ("dm-1", "决策", "decision_maker"),
                ("aud-1", "审计", "auditor")):
                setup.create_user(uid, name, role)
            digest = setup.register_snapshot("res-1", snapshot_payload())["content_sha256"]
            plans = {}
            for plan_id, production in (
                ("plan-a", ["42", "56", "48", "40", "32"]),
                ("plan-b", ["60", "90", "88", "76", "62"]),
            ):
                groups = (["wg-north-ridge"] if plan_id == "plan-a"
                          else ["wg-north-ridge", "wg-south-flank"])
                setup.create_plan("res-1", plan_payload(
                    plan_id, digest, name=plan_id, groups=groups,
                    capex_items=["capex-drilling-north", "capex-drilling-south",
                                 "capex-fpso-retrofit"],
                    production=production))
                setup.evaluate_plan("fin-1", plan_id)
            setup_connection.close()

            barrier = threading.Barrier(2)
            outcomes: list[str] = []
            outcomes_lock = threading.Lock()

            def approve(plan_id: str) -> None:
                connection = connect(database)
                try:
                    service = GovernanceService(connection)
                    barrier.wait(timeout=10)
                    try:
                        service.decide("dm-1", plan_id, "approved",
                                       f"并发批准 {plan_id}", expected_revision=1)
                        with outcomes_lock:
                            outcomes.append(f"{plan_id}:ok")
                    except Exception as exc:  # noqa: BLE001
                        with outcomes_lock:
                            outcomes.append(f"{plan_id}:{type(exc).__name__}")
                finally:
                    connection.close()

            threads = [threading.Thread(target=approve, args=(plan_id,))
                       for plan_id in ("plan-a", "plan-b")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
                self.assertFalse(thread.is_alive())

            self.assertEqual(len(outcomes), 2)
            self.assertEqual(sum(1 for item in outcomes if item.endswith(":ok")), 1)

            check = connect(database)
            try:
                active = check.execute(
                    "SELECT count(*) AS c FROM investment_decisions "
                    "WHERE asset_id='liuhua-deepwater' AND outcome='approved' "
                    "AND superseded_by IS NULL").fetchone()["c"]
                self.assertEqual(active, 1)
            finally:
                check.close()


if __name__ == "__main__":
    unittest.main()
