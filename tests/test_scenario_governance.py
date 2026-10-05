from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scenario_governance.api import JsonApplication
from scenario_governance.clock import FrozenClock
from scenario_governance.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from scenario_governance.jsonio import load_json
from scenario_governance.service import GovernanceService


ROOT = Path(__file__).resolve().parents[1]
DEMO = load_json(ROOT / "fixtures" / "demo_redevelopment_inputs.json")
PROJECT = DEMO["project"]["project_id"]

SUBMITTERS = {
    "reserves_version": "reservoir",
    "well_group_response": "reservoir",
    "water_cut_forecast": "reservoir",
    "facility_bottleneck": "facility",
    "shutdown_window": "facility",
    "capex": "facility",
    "oil_price_assumption": "econ",
}


class GovernanceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        self.service = GovernanceService(self.connection, self.clock)
        for user_id, role in (
            ("reservoir", "reservoir_engineer"),
            ("facility", "facility_engineer"),
            ("econ", "economist"),
            ("ops", "operations"),
            ("dm", "decision_maker"),
            ("dm2", "decision_maker"),
            ("auditor", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        project = DEMO["project"]
        self.service.register_project("dm", project["project_id"], project["field_name"], project["source_pattern"])

    def tearDown(self) -> None:
        self.connection.close()

    def submit_all(self) -> dict[str, str]:
        selections = {}
        for item in DEMO["contributions"]:
            self.service.submit_contribution(
                SUBMITTERS[item["category"]], PROJECT, item["category"], item["version"], item["payload"]
            )
            selections[item["category"]] = item["version"]
        return selections

    def compose(self) -> dict[str, object]:
        return self.service.compose_snapshot("reservoir", "snapshot-1", PROJECT, self.submit_all())

    def create_and_evaluate(self) -> dict[str, dict[str, object]]:
        snapshot = self.compose()
        evaluated = {}
        for item in DEMO["scenarios"]:
            self.service.create_scenario(
                "reservoir", item["scenario_id"], PROJECT, snapshot["snapshot_id"], item["definition"]
            )
            evaluated[item["scenario_id"]] = self.service.evaluate_scenario("econ", item["scenario_id"])
        return evaluated


class ContributionTests(GovernanceTestCase):
    def test_contributions_are_content_addressed(self) -> None:
        result = self.service.submit_contribution(
            "reservoir", PROJECT, "reserves_version", "2026.1",
            DEMO["contributions"][0]["payload"],
        )
        self.assertEqual(len(result["content_sha256"]), 64)
        with self.assertRaises(Conflict):
            self.service.submit_contribution(
                "reservoir", PROJECT, "reserves_version", "2026.1",
                DEMO["contributions"][0]["payload"],
            )

    def test_contribution_permission_follows_team(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.submit_contribution("econ", PROJECT, "reserves_version", "1", {"x": 1})
        with self.assertRaises(Forbidden):
            self.service.submit_contribution("reservoir", PROJECT, "oil_price_assumption", "1", {"x": 1})
        with self.assertRaises(Forbidden):
            self.service.submit_contribution("ops", PROJECT, "capex", "1", {"x": 1})

    def test_contribution_payload_is_validated(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.submit_contribution(
                "reservoir", PROJECT, "water_cut_forecast", "1",
                {"current_water_cut_percent": "80", "annual_increase_points": "1", "ceiling_percent": "70"},
            )

    def test_contributions_cannot_be_modified_or_deleted(self) -> None:
        self.submit_all()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("UPDATE input_contributions SET version='9.9' LIMIT 1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM input_contributions LIMIT 1")


class SnapshotTests(GovernanceTestCase):
    def test_snapshot_requires_all_seven_categories(self) -> None:
        selections = self.submit_all()
        del selections["capex"]
        with self.assertRaises(ValidationFailed) as ctx:
            self.service.compose_snapshot("reservoir", "snapshot-1", PROJECT, selections)
        self.assertIn("资本支出", str(ctx.exception))

    def test_snapshot_rejects_unknown_category_and_version(self) -> None:
        selections = self.submit_all()
        with self.assertRaises(ValidationFailed):
            self.service.compose_snapshot("reservoir", "snapshot-1", PROJECT, selections | {"extra": "1"})
        with self.assertRaises(NotFound):
            self.service.compose_snapshot(
                "reservoir", "snapshot-1", PROJECT, selections | {"capex": "2099.0"}
            )

    def test_snapshot_is_immutable_and_content_unique(self) -> None:
        snapshot = self.compose()
        self.assertEqual(len(snapshot["content_sha256"]), 64)
        with self.assertRaises(Conflict):
            self.service.compose_snapshot("facility", "snapshot-2", PROJECT, self._selections())
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("UPDATE input_snapshots SET project_id='other' LIMIT 1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM input_snapshots LIMIT 1")

    def _selections(self) -> dict[str, str]:
        return {item["category"]: item["version"] for item in DEMO["contributions"]}


class ScenarioEvaluationTests(GovernanceTestCase):
    def test_scenario_validates_against_snapshot(self) -> None:
        snapshot = self.compose()
        with self.assertRaises(ValidationFailed):
            self.service.create_scenario("reservoir", "bad-1", PROJECT, snapshot["snapshot_id"], {
                "name": "未知井组", "infill_wells": [{"well_group": "Z9", "count": 1}], "horizon_years": 10,
            })
        with self.assertRaises(ValidationFailed):
            self.service.create_scenario("reservoir", "bad-2", PROJECT, snapshot["snapshot_id"], {
                "name": "超上限", "infill_wells": [{"well_group": "A1", "count": 99}], "horizon_years": 10,
            })
        with self.assertRaises(ValidationFailed):
            self.service.create_scenario("reservoir", "bad-3", PROJECT, snapshot["snapshot_id"], {
                "name": "空方案", "horizon_years": 10,
            })

    def test_scenario_definition_is_immutable(self) -> None:
        snapshot = self.compose()
        self.service.create_scenario(
            "reservoir", "infill-only", PROJECT, snapshot["snapshot_id"], DEMO["scenarios"][0]["definition"]
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("UPDATE scenarios SET name='改写' LIMIT 1")

    def test_evaluation_is_deterministic_and_replayed(self) -> None:
        evaluated = self.create_and_evaluate()
        first = evaluated["infill-plus-upgrade"]
        replay = self.service.evaluate_scenario("econ", "infill-plus-upgrade")
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["evaluation_id"], replay["evaluation_id"])
        self.assertEqual(first["result"], replay["result"])
        self.assertEqual(len(first["input_sha256"]), 64)

    def test_evaluation_reports_uplift_risk_payback_and_sensitivity(self) -> None:
        evaluated = self.create_and_evaluate()
        upgrade = evaluated["infill-plus-upgrade"]["result"]
        infill = evaluated["infill-only"]["result"]
        self.assertGreater(float(upgrade["uplift_kl"]), float(infill["uplift_kl"]))
        self.assertIsNotNone(upgrade["payback_years"])
        self.assertIsNone(infill["payback_years"])
        self.assertTrue(infill["payback_beyond_horizon"])
        self.assertIn(upgrade["risk"]["classification"], {"low", "medium", "high"})
        self.assertTrue(upgrade["risk"]["binding_constraints"])
        cases = upgrade["sensitivity"]["cases"]
        self.assertEqual(
            sorted(cases), ["capex_up_20", "oil_price_down_20", "oil_price_up_20", "response_down_20"]
        )
        self.assertLess(
            float(cases["oil_price_down_20"]["npv_m_cny"]), float(upgrade["npv_m_cny"])
        )
        self.assertLess(
            float(upgrade["sensitivity"]["break_even_oil_price_cny_per_kl"]),
            float(upgrade["annual"][0]["price_cny_per_kl"]),
        )
        self.assertTrue(upgrade["reserves_check"]["within_reserves"])

    def test_compare_ranks_by_npv(self) -> None:
        self.create_and_evaluate()
        comparison = self.service.compare_scenarios("dm", PROJECT, ["infill-only", "infill-plus-upgrade"])
        self.assertEqual(comparison["scenarios"][0]["scenario_id"], "infill-plus-upgrade")
        self.assertEqual(comparison["scenarios"][0]["rank"], 1)
        self.assertEqual(comparison["scenarios"][1]["rank"], 2)
        with self.assertRaises(NotFound):
            self.service.compare_scenarios("dm", PROJECT, ["infill-only", "missing"])
        snapshot = self.get_snapshot_id()
        self.service.create_scenario("reservoir", "unevaluated", PROJECT, snapshot, {
            "name": "未评价方案", "infill_wells": [{"well_group": "C3", "count": 2}], "horizon_years": 5,
        })
        with self.assertRaises(InvalidState):
            self.service.compare_scenarios("dm", PROJECT, ["infill-only", "unevaluated"])
        with self.assertRaises(ValidationFailed):
            self.service.compare_scenarios("dm", PROJECT, "infill-only")

    def get_snapshot_id(self) -> str:
        row = self.connection.execute("SELECT snapshot_id FROM input_snapshots LIMIT 1").fetchone()
        return row["snapshot_id"]


class DecisionTests(GovernanceTestCase):
    def test_decision_pins_versions_and_keeps_dissents(self) -> None:
        evaluated = self.create_and_evaluate()
        winner = evaluated["infill-plus-upgrade"]
        decision = self.service.decide(
            "dm", PROJECT, "infill-plus-upgrade", winner["evaluation_id"], "approved",
            "满足立项要求", 0,
            dissents=[{"author_id": "reservoir", "opinion": "B2 井组置信度偏低"}],
        )
        self.assertEqual(decision["decision_revision"], 1)
        self.assertEqual(decision["status"], "effective")
        snapshot = self.connection.execute(
            "SELECT content_sha256 FROM input_snapshots WHERE snapshot_id='snapshot-1'"
        ).fetchone()
        self.assertEqual(decision["snapshot_sha256"], snapshot["content_sha256"])
        dissents = self.connection.execute("SELECT * FROM dissents WHERE decision_id=?",
                                           (decision["decision_id"],)).fetchall()
        self.assertEqual(len(dissents), 1)
        self.assertEqual(dissents[0]["author_id"], "reservoir")

    def test_concurrent_decisions_have_single_effective_revision(self) -> None:
        evaluated = self.create_and_evaluate()
        winner = evaluated["infill-plus-upgrade"]
        first = self.service.decide(
            "dm", PROJECT, "infill-plus-upgrade", winner["evaluation_id"], "approved", "先批", 0
        )
        with self.assertRaises(Conflict):
            self.service.decide(
                "dm2", PROJECT, "infill-plus-upgrade", winner["evaluation_id"], "approved", "并发抢批", 0
            )
        effective = self.connection.execute(
            "SELECT count(*) FROM decisions WHERE project_id=? AND status='effective'", (PROJECT,)
        ).fetchone()[0]
        self.assertEqual(effective, 1)
        self.assertEqual(first["decision_revision"], 1)

    def test_revision_chain_supersedes_without_rewriting(self) -> None:
        evaluated = self.create_and_evaluate()
        winner = evaluated["infill-plus-upgrade"]
        first = self.service.decide(
            "dm", PROJECT, "infill-plus-upgrade", winner["evaluation_id"], "approved", "第一版结论", 0
        )
        second = self.service.decide(
            "dm", PROJECT, "infill-only", evaluated["infill-only"]["evaluation_id"], "rejected",
            "复核后否决仅井网方案", first["decision_revision"]
        )
        self.assertEqual(second["decision_revision"], 2)
        rows = self.connection.execute(
            "SELECT decision_revision,decision,status,rationale FROM decisions WHERE project_id=? "
            "ORDER BY decision_revision", (PROJECT,)
        ).fetchall()
        self.assertEqual([(row["decision_revision"], row["status"]) for row in rows],
                         [(1, "superseded"), (2, "effective")])
        self.assertEqual(rows[0]["rationale"], "第一版结论")
        lineage = self.service.trace_decision("auditor", first["decision_id"])["decision_lineage"]
        self.assertEqual([row["decision_revision"] for row in lineage], [1, 2])

    def test_database_enforces_single_effective_revision(self) -> None:
        evaluated = self.create_and_evaluate()
        winner = evaluated["infill-plus-upgrade"]
        decision = self.service.decide(
            "dm", PROJECT, "infill-plus-upgrade", winner["evaluation_id"], "approved", "定稿", 0
        )
        row = self.connection.execute(
            "SELECT * FROM decisions WHERE decision_id=?", (decision["decision_id"],)
        ).fetchone()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO decisions(project_id,decision_revision,scenario_id,evaluation_id,"
                "snapshot_sha256,scenario_sha256,decision,rationale,status,decided_by,decided_at) "
                "VALUES(?,?,?,?,?,?,?,?,'effective',?,?)",
                (PROJECT, 2, row["scenario_id"], row["evaluation_id"], row["snapshot_sha256"],
                 row["scenario_sha256"], "approved", "绕过服务层的并发批准", "dm2",
                 row["decided_at"]),
            )
        self.connection.rollback()
        effective = self.connection.execute(
            "SELECT count(*) FROM decisions WHERE project_id=? AND status='effective'", (PROJECT,)
        ).fetchone()[0]
        self.assertEqual(effective, 1)

    def test_decision_rejects_malformed_dissents(self) -> None:
        evaluated = self.create_and_evaluate()
        winner = evaluated["infill-plus-upgrade"]
        with self.assertRaises(ValidationFailed):
            self.service.decide(
                "dm", PROJECT, "infill-plus-upgrade", winner["evaluation_id"], "approved",
                "定稿", 0, dissents=["不是对象"]
            )
        with self.assertRaises(NotFound):
            self.service.decide(
                "dm", PROJECT, "infill-plus-upgrade", winner["evaluation_id"], "approved",
                "定稿", 0, dissents=[{"author_id": "ghost", "opinion": "查无此人"}]
            )

    def test_decision_requires_matching_evaluation_and_revision(self) -> None:
        evaluated = self.create_and_evaluate()
        with self.assertRaises(NotFound):
            self.service.decide(
                "dm", PROJECT, "infill-only", evaluated["infill-plus-upgrade"]["evaluation_id"],
                "approved", "评价与方案不匹配", 0
            )
        with self.assertRaises(Forbidden):
            self.service.decide(
                "reservoir", PROJECT, "infill-only", evaluated["infill-only"]["evaluation_id"],
                "approved", "越权", 0
            )

    def test_decision_content_cannot_be_rewritten(self) -> None:
        evaluated = self.create_and_evaluate()
        winner = evaluated["infill-plus-upgrade"]
        self.service.decide("dm", PROJECT, "infill-plus-upgrade", winner["evaluation_id"],
                            "approved", "定稿", 0)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("UPDATE decisions SET rationale='改写历史' LIMIT 1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM decisions LIMIT 1")

    def test_dissent_can_be_appended_without_changing_decision(self) -> None:
        evaluated = self.create_and_evaluate()
        winner = evaluated["infill-plus-upgrade"]
        decision = self.service.decide(
            "dm", PROJECT, "infill-plus-upgrade", winner["evaluation_id"], "approved", "定稿", 0
        )
        appended = self.service.record_dissent("facility", decision["decision_id"], "改造窗口与检修冲突")
        self.assertEqual(appended["decision_id"], decision["decision_id"])
        row = self.connection.execute("SELECT rationale FROM decisions WHERE decision_id=?",
                                      (decision["decision_id"],)).fetchone()
        self.assertEqual(row["rationale"], "定稿")
        trace = self.service.trace_decision("auditor", decision["decision_id"])
        self.assertEqual(len(trace["dissents"]), 1)


class ActualDeviationTests(GovernanceTestCase):
    def approve_winner(self) -> dict[str, object]:
        evaluated = self.create_and_evaluate()
        winner = evaluated["infill-plus-upgrade"]
        return self.service.decide(
            "dm", PROJECT, "infill-plus-upgrade", winner["evaluation_id"], "approved", "定稿", 0
        )

    def test_actual_generates_deviation_only(self) -> None:
        decision = self.approve_winner()
        before = self.connection.execute("SELECT * FROM decisions").fetchall()
        actual = self.service.record_actual("ops", PROJECT, 1, DEMO["actual_year1"]["metrics"])
        self.assertIsNotNone(actual["deviation"])
        self.assertEqual(actual["deviation"]["decision_id"], decision["decision_id"])
        after = self.connection.execute("SELECT * FROM decisions").fetchall()
        self.assertEqual([dict(row) for row in before], [dict(row) for row in after])
        variance = actual["deviation"]["variance"]
        self.assertFalse(variance["beyond_sensitivity_boundary"])
        self.assertIn("oil_kl", variance["metrics"])

    def test_actual_beyond_sensitivity_boundary_is_flagged(self) -> None:
        self.approve_winner()
        actual = self.service.record_actual("ops", PROJECT, 1, {
            "actual_oil_kl": "100000",
            "actual_water_cut_percent": "85",
            "actual_capex_to_date_m_cny": "1400",
            "actual_oil_price_cny_per_kl": "400",
        })
        variance = actual["deviation"]["variance"]
        self.assertTrue(variance["beyond_sensitivity_boundary"])
        self.assertFalse(variance["metrics"]["oil_kl"]["within_sensitivity_boundary"])
        self.assertFalse(variance["metrics"]["capex_to_date_m_cny"]["within_sensitivity_boundary"])

    def test_actual_is_immutable_per_year(self) -> None:
        self.approve_winner()
        self.service.record_actual("ops", PROJECT, 1, DEMO["actual_year1"]["metrics"])
        with self.assertRaises(Conflict):
            self.service.record_actual("ops", PROJECT, 1, DEMO["actual_year1"]["metrics"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("UPDATE actuals SET horizon_year=9 LIMIT 1")

    def test_actual_beyond_evaluation_horizon_is_rejected(self) -> None:
        self.approve_winner()
        with self.assertRaises(ValidationFailed):
            self.service.record_actual("ops", PROJECT, 11, DEMO["actual_year1"]["metrics"])

    def test_actual_without_effective_decision_records_no_deviation(self) -> None:
        self.create_and_evaluate()
        actual = self.service.record_actual("ops", PROJECT, 1, DEMO["actual_year1"]["metrics"])
        self.assertIsNone(actual["deviation"])

    def test_derive_scenario_from_actual_keeps_lineage(self) -> None:
        self.approve_winner()
        actual = self.service.record_actual("ops", PROJECT, 1, DEMO["actual_year1"]["metrics"])
        derived = self.service.derive_scenario("reservoir", "round-2", actual["actual_id"], {
            "name": "实绩修正版",
            "infill_wells": [{"well_group": "C3", "count": 4}],
            "horizon_years": 8,
        })
        self.assertEqual(derived["derived_from_actual_id"], actual["actual_id"])
        self.assertEqual(derived["origin_scenario_id"], "infill-plus-upgrade")
        row = self.connection.execute(
            "SELECT derived_from_actual_id FROM scenarios WHERE scenario_id='round-2'"
        ).fetchone()
        self.assertEqual(row["derived_from_actual_id"], actual["actual_id"])

    def test_derive_scenario_requires_deviation_lineage(self) -> None:
        self.create_and_evaluate()
        actual = self.service.record_actual("ops", PROJECT, 1, DEMO["actual_year1"]["metrics"])
        with self.assertRaises(InvalidState):
            self.service.derive_scenario("reservoir", "round-2", actual["actual_id"], {
                "name": "无投决派生", "infill_wells": [{"well_group": "C3", "count": 2}], "horizon_years": 5,
            })


class TraceabilityTests(GovernanceTestCase):
    def test_trace_reconstructs_full_evidence_chain(self) -> None:
        evaluated = self.create_and_evaluate()
        winner = evaluated["infill-plus-upgrade"]
        decision = self.service.decide(
            "dm", PROJECT, "infill-plus-upgrade", winner["evaluation_id"], "approved",
            "定稿", 0, dissents=[{"author_id": "econ", "opinion": "油价假设偏乐观"}],
        )
        self.service.record_actual("ops", PROJECT, 1, DEMO["actual_year1"]["metrics"])
        trace = self.service.trace_decision("auditor", decision["decision_id"])
        self.assertEqual(trace["decision"]["decision"], "approved")
        self.assertEqual(len(trace["contributions"]), 7)
        categories = {row["category"] for row in trace["contributions"]}
        self.assertEqual(
            categories,
            {"reserves_version", "well_group_response", "water_cut_forecast", "facility_bottleneck",
             "shutdown_window", "capex", "oil_price_assumption"},
        )
        for contribution in trace["contributions"]:
            self.assertEqual(len(contribution["content_sha256"]), 64)
            self.assertTrue(contribution["payload"])
        self.assertEqual(trace["snapshot"]["content_sha256"], decision["snapshot_sha256"])
        self.assertEqual(trace["evaluation"]["evaluation_id"], winner["evaluation_id"])
        self.assertIn("sensitivity", trace["evaluation"]["result"])
        self.assertEqual(len(trace["dissents"]), 1)
        self.assertEqual(len(trace["deviations"]), 1)
        self.assertTrue(trace["events"])
        with self.assertRaises(Forbidden):
            self.service.trace_decision("ops", decision["decision_id"])

    def test_audit_chain_detects_tampering(self) -> None:
        self.create_and_evaluate()
        self.assertTrue(self.service.audit_chain("auditor")["valid"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM audit_events WHERE event_id=1")
        # 绕过触发器直接篡改载荷，哈希链校验必须发现
        self.connection.execute("DROP TRIGGER IF EXISTS audit_no_update")
        self.connection.execute("UPDATE audit_events SET payload_json='{}' WHERE event_id=2")
        self.assertFalse(self.service.audit_chain("auditor")["valid"])

    def test_audit_events_are_append_only(self) -> None:
        self.create_and_evaluate()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM audit_events WHERE event_id=1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("UPDATE audit_events SET actor_id='nobody' WHERE event_id=1")


class ProjectLifecycleTests(GovernanceTestCase):
    def test_closed_project_rejects_new_inputs_and_decisions(self) -> None:
        evaluated = self.create_and_evaluate()
        self.service.close_project("dm", PROJECT)
        with self.assertRaises(InvalidState):
            self.service.submit_contribution(
                "reservoir", PROJECT, "reserves_version", "2026.2",
                DEMO["contributions"][0]["payload"],
            )
        with self.assertRaises(InvalidState):
            self.service.decide(
                "dm", PROJECT, "infill-only", evaluated["infill-only"]["evaluation_id"],
                "rejected", "已关闭", 0
            )
        with self.assertRaises(InvalidState):
            self.service.close_project("dm", PROJECT)
        trace = self.service.list_decisions("auditor", PROJECT)
        self.assertEqual(trace["decisions"], [])


class ApiTests(GovernanceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.app = JsonApplication(self.service)

    def post(self, path: str, actor: str, payload: dict[str, object]):
        return self.app.handle(
            "POST", path, {"X-Actor-Id": "dm"}, json.dumps(payload).encode("utf-8")
        ) if actor == "dm" else self.app.handle(
            "POST", path, {"X-Actor-Id": actor}, json.dumps(payload).encode("utf-8")
        )

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)

    def test_full_governance_flow_over_http(self) -> None:
        headers = {"X-Actor-Id": "dm"}
        body = json.dumps({
            "project_id": "hz-2", "field_name": "另一座深水老油田", "source_pattern": "liuhua",
        }).encode("utf-8")
        response = self.app.handle("POST", "/projects", headers, body)
        self.assertEqual(response.status, 201)
        for item in DEMO["contributions"]:
            response = self.app.handle(
                "POST", f"/projects/hz-2/contributions", {"X-Actor-Id": SUBMITTERS[item["category"]]},
                json.dumps({"category": item["category"], "version": item["version"],
                            "payload": item["payload"]}, default=str).encode("utf-8"),
            )
            self.assertEqual(response.status, 201, response.body)
        response = self.app.handle(
            "POST", "/projects/hz-2/snapshots", {"X-Actor-Id": "reservoir"},
            json.dumps({"snapshot_id": "snap-http",
                        "selections": {i["category"]: i["version"] for i in DEMO["contributions"]}}).encode("utf-8"),
        )
        self.assertEqual(response.status, 201, response.body)
        scenario = DEMO["scenarios"][1]
        response = self.app.handle(
            "POST", "/projects/hz-2/scenarios", {"X-Actor-Id": "facility"},
            json.dumps({"scenario_id": "http-scenario", "snapshot_id": "snap-http",
                        "definition": scenario["definition"]}, default=str).encode("utf-8"),
        )
        self.assertEqual(response.status, 201, response.body)
        response = self.app.handle("POST", "/scenarios/http-scenario/evaluate", {"X-Actor-Id": "econ"})
        self.assertEqual(response.status, 200, response.body)
        evaluation_id = response.body["evaluation_id"]
        response = self.app.handle(
            "POST", "/projects/hz-2/decisions", headers,
            json.dumps({"scenario_id": "http-scenario", "evaluation_id": evaluation_id,
                        "decision": "approved", "rationale": "HTTP 全流程",
                        "expected_decision_revision": 0,
                        "dissents": [{"author_id": "econ", "opinion": "保留意见"}]}).encode("utf-8"),
        )
        self.assertEqual(response.status, 201, response.body)
        decision_id = response.body["decision_id"]
        response = self.app.handle("GET", f"/decisions/{decision_id}/trace", {"X-Actor-Id": "auditor"})
        self.assertEqual(response.status, 200)
        self.assertEqual(len(response.body["contributions"]), 7)
        response = self.app.handle("GET", "/audit/chain", {"X-Actor-Id": "auditor"})
        self.assertTrue(response.body["valid"])

    def test_error_shape_and_missing_actor(self) -> None:
        response = self.app.handle("POST", "/projects", body=b"not-json")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")
        response = self.app.handle("POST", "/projects", body=b"{}")
        self.assertEqual(response.status, 422)
        response = self.app.handle("GET", "/decisions/1/trace", {"X-Actor-Id": "auditor"})
        self.assertEqual(response.status, 404)
        response = self.app.handle("GET", "/nowhere", {"X-Actor-Id": "auditor"})
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body["error"]["code"], "route_not_found")


if __name__ == "__main__":
    unittest.main()
