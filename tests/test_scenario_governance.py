from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from scenario_governance.acceptance import plan_payload, snapshot_payload
from scenario_governance.clock import FrozenClock
from scenario_governance.contracts import InputSnapshot, ValidationError
from scenario_governance.economics import EVALUATION_VERSION, evaluate
from scenario_governance.jsonio import canonical_json, content_digest
from scenario_governance.service import GovernanceService
from scenario_governance.storage import connect


def make_service() -> GovernanceService:
    connection = connect(":memory:")
    service = GovernanceService(
        connection, FrozenClock(datetime(2026, 10, 5, tzinfo=timezone.utc)))
    service.create_user("res-1", "油藏", "reservoir")
    service.create_user("fac-1", "工程", "facility")
    service.create_user("fin-1", "财务", "finance")
    service.create_user("dm-1", "决策", "decision_maker")
    service.create_user("aud-1", "审计", "auditor")
    return service


def snapshot_v1(service: GovernanceService) -> dict:
    return service.register_snapshot("res-1", snapshot_payload())


def balanced_plan(service: GovernanceService, digest: str, plan_id: str = "plan-balanced") -> dict:
    service.create_plan("res-1", plan_payload(
        plan_id, digest,
        name="南北联合调整",
        groups=["wg-north-ridge", "wg-south-flank"],
        capex_items=["capex-drilling-north", "capex-drilling-south", "capex-fpso-retrofit"],
        production=["60", "90", "88", "76", "62"],
    ))
    return service.evaluate_plan("fin-1", plan_id)


def approve(service: GovernanceService, plan_id: str = "plan-balanced") -> dict:
    return service.decide(
        "dm-1", plan_id, "approved", "同意实施", expected_revision=1,
        objections=[{"raised_by": "fac-1", "content": "改造窗口紧张"}])


class SnapshotContractTests(unittest.TestCase):
    def test_seven_sections_required(self) -> None:
        raw = snapshot_payload()
        snapshot = InputSnapshot.from_dict(raw)
        self.assertEqual(snapshot.horizon_years, 5)
        self.assertEqual(len(snapshot.well_groups), 2)
        self.assertEqual(len(snapshot.capex), 3)

    def test_attestations_must_cover_all_sections(self) -> None:
        raw = snapshot_payload()
        del raw["attestations"]["capex"]
        with self.assertRaisesRegex(ValidationError, "必须覆盖且仅覆盖"):
            InputSnapshot.from_dict(raw)

    def test_bottleneck_capex_reference_must_exist(self) -> None:
        raw = snapshot_payload()
        raw["facility_constraints"][0]["debottleneck_capex_item"] = "missing-item"
        with self.assertRaisesRegex(ValidationError, "资本支出项"):
            InputSnapshot.from_dict(raw)

    def test_outage_facility_must_exist(self) -> None:
        raw = snapshot_payload()
        raw["outage_windows"][0]["facility_id"] = "unknown-facility"
        with self.assertRaisesRegex(ValidationError, "未声明的设施"):
            InputSnapshot.from_dict(raw)

    def test_price_low_base_high_order(self) -> None:
        raw = snapshot_payload()
        raw["oil_price"]["low_usd_bbl"] = "90"
        with self.assertRaisesRegex(ValidationError, "low <= base <= high"):
            InputSnapshot.from_dict(raw)

    def test_outage_window_order(self) -> None:
        raw = snapshot_payload()
        raw["outage_windows"][0]["end_date"] = "2028-02-01"
        with self.assertRaisesRegex(ValidationError, "不能早于"):
            InputSnapshot.from_dict(raw)

    def test_digest_is_deterministic(self) -> None:
        raw = snapshot_payload()
        reordered = dict(raw)
        self.assertEqual(content_digest([raw]), content_digest([reordered]))
        self.assertEqual(len(content_digest([raw])), 64)


class SnapshotGovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = make_service()

    def test_attestation_role_enforced(self) -> None:
        raw = snapshot_payload()
        raw["attestations"]["capex"] = "res-1"  # 油藏替财务背书
        with self.assertRaisesRegex(Exception, "capex"):
            self.service.register_snapshot("res-1", raw)

    def test_decision_maker_cannot_register_snapshot(self) -> None:
        with self.assertRaisesRegex(Exception, "无权"):
            self.service.register_snapshot("dm-1", snapshot_payload())

    def test_same_content_is_conflict(self) -> None:
        self.service.register_snapshot("res-1", snapshot_payload())
        with self.assertRaisesRegex(Exception, "冲突"):
            self.service.register_snapshot("res-1", snapshot_payload())

    def test_changed_inputs_create_new_revision(self) -> None:
        first = self.service.register_snapshot("res-1", snapshot_payload())
        second = self.service.register_snapshot("res-1", snapshot_payload(price_base="70"))
        self.assertEqual(first["revision"], 1)
        self.assertEqual(second["revision"], 2)
        self.assertNotEqual(first["content_sha256"], second["content_sha256"])


class PlanEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = make_service()
        self.digest = snapshot_v1(self.service)["content_sha256"]

    def test_plan_must_reference_known_snapshot(self) -> None:
        raw = plan_payload(
            "plan-x", "a" * 64, name="x", groups=["wg-north-ridge"],
            capex_items=["capex-drilling-north"], production=["42", "56", "48", "40", "32"])
        with self.assertRaisesRegex(Exception, "输入快照版本不存在"):
            self.service.create_plan("res-1", raw)

    def test_plan_cannot_select_unknown_well_group(self) -> None:
        raw = plan_payload(
            "plan-x", self.digest, name="x", groups=["wg-ghost"],
            capex_items=["capex-drilling-north"], production=["42", "56", "48", "40", "32"])
        with self.assertRaisesRegex(Exception, "不存在的井组"):
            self.service.create_plan("res-1", raw)

    def test_evaluation_is_replayable_and_versioned(self) -> None:
        first = balanced_plan(self.service, self.digest)
        second = self.service.evaluate_plan("fin-1", "plan-balanced")
        self.assertTrue(second["replayed"])
        self.assertEqual(first["result"], second["result"])
        self.assertEqual(second["evaluation_version"], EVALUATION_VERSION)

    def test_outage_window_reduces_year_two_oil(self) -> None:
        evaluation = balanced_plan(self.service, self.digest)["result"]
        year1 = Decimal(evaluation["yearly_base"][0]["outage_factor"])
        year2 = Decimal(evaluation["yearly_base"][1]["outage_factor"])
        self.assertEqual(year1, Decimal("1.0000"))
        self.assertLess(year2, Decimal("1.0000"))
        self.assertGreater(Decimal(evaluation["totals"]["outage_loss_oil_10kt"]), Decimal(0))

    def test_compare_requires_evaluated_plans(self) -> None:
        self.service.create_plan("res-1", plan_payload(
            "plan-a", self.digest, name="a", groups=["wg-north-ridge"],
            capex_items=["capex-drilling-north"], production=["42", "56", "48", "40", "32"]))
        with self.assertRaisesRegex(Exception, "尚未评价"):
            self.service.compare_plans("dm-1", ["plan-a"])

    def test_evaluation_is_deterministic_across_instances(self) -> None:
        snapshot = InputSnapshot.from_dict(snapshot_payload())
        raw = plan_payload(
            "plan-a", self.digest, name="a", groups=["wg-north-ridge"],
            capex_items=["capex-drilling-north"], production=["42", "56", "48", "40", "32"])
        from scenario_governance.contracts import DevelopmentPlan
        plan = DevelopmentPlan.from_dict(raw, snapshot)
        r1 = evaluate(plan, snapshot)
        r2 = evaluate(plan, snapshot)
        self.assertEqual(canonical_json(r1), canonical_json(r2))


class DecisionGovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = make_service()
        self.digest = snapshot_v1(self.service)["content_sha256"]
        balanced_plan(self.service, self.digest)
        approve(self.service)

    def test_decision_requires_pinned_revision(self) -> None:
        self.service.create_plan("res-1", plan_payload(
            "plan-c", self.digest, name="c", groups=["wg-north-ridge"],
            capex_items=["capex-drilling-north"], production=["42", "56", "48", "40", "32"]))
        with self.assertRaisesRegex(Exception, "修订版本"):
            self.service.decide("dm-1", "plan-c", "approved", "x", expected_revision=9)

    def test_old_decision_is_immutable(self) -> None:
        with self.assertRaisesRegex(Exception, "不可改写"):
            approve(self.service)

    def test_only_one_active_approval_and_supersession_chain(self) -> None:
        digest_v2 = self.service.register_snapshot(
            "res-1", snapshot_payload(price_base="68"))["content_sha256"]
        self.service.create_plan("res-1", plan_payload(
            "plan-revised", digest_v2, name="修订",
            groups=["wg-north-ridge", "wg-south-flank"],
            capex_items=["capex-drilling-north", "capex-drilling-south", "capex-fpso-retrofit"],
            production=["58", "82", "84", "74", "60"]))
        self.service.evaluate_plan("fin-1", "plan-revised")
        new_decision = self.service.decide(
            "dm-1", "plan-revised", "approved", "取代旧案", expected_revision=1,
            expected_active_decision_id=1)
        self.assertEqual(new_decision["superseded_decision_id"], 1)
        active = self.service.active_decision("aud-1", "liuhua-deepwater")
        self.assertEqual(active["plan_id"], "plan-revised")
        old = self.service.connection.execute(
            "SELECT * FROM investment_decisions WHERE decision_id=1").fetchone()
        self.assertEqual(old["superseded_by"], new_decision["decision_id"])
        self.assertEqual(self.service.get_plan("plan-balanced")["state"], "superseded")

    def test_supersession_requires_explicit_current_decision(self) -> None:
        digest_v2 = self.service.register_snapshot(
            "res-1", snapshot_payload(price_base="68"))["content_sha256"]
        self.service.create_plan("res-1", plan_payload(
            "plan-stale", digest_v2, name="未审阅当前投决",
            groups=["wg-north-ridge", "wg-south-flank"],
            capex_items=["capex-drilling-north", "capex-drilling-south", "capex-fpso-retrofit"],
            production=["58", "82", "84", "74", "60"]))
        self.service.evaluate_plan("fin-1", "plan-stale")
        # 已有生效批准 #1，但未显式引用它：拒绝
        with self.assertRaisesRegex(Exception, "生效投决已变化"):
            self.service.decide(
                "dm-1", "plan-stale", "approved", "无意取代", expected_revision=1)
        # 显式引用一个不存在的投决号：同样拒绝
        with self.assertRaisesRegex(Exception, "生效投决已变化"):
            self.service.decide(
                "dm-1", "plan-stale", "approved", "错误引用", expected_revision=1,
                expected_active_decision_id=999)

    def test_rejection_does_not_supersede_active_approval(self) -> None:
        digest_v2 = self.service.register_snapshot(
            "res-1", snapshot_payload(price_base="56"))["content_sha256"]
        self.service.create_plan("res-1", plan_payload(
            "plan-low", digest_v2, name="低价案",
            groups=["wg-north-ridge"],
            capex_items=["capex-drilling-north"], production=["42", "56", "48", "40", "32"]))
        self.service.evaluate_plan("fin-1", "plan-low")
        rejected = self.service.decide(
            "dm-1", "plan-low", "rejected", "低价下经济性不足", expected_revision=1)
        self.assertIsNone(rejected["superseded_decision_id"])
        active = self.service.active_decision("aud-1", "liuhua-deepwater")
        self.assertEqual(active["plan_id"], "plan-balanced")

    def test_objections_retained_with_decision(self) -> None:
        dossier = self.service.dossier("aud-1", "plan-balanced")
        objections = dossier["decisions"][0]["objections"]
        self.assertEqual(len(objections), 1)
        self.assertEqual(objections[0]["raised_by"], "fac-1")
        self.service.add_objection("fin-1", "plan-balanced", "事后补充敏感性担忧")
        dossier = self.service.dossier("aud-1", "plan-balanced")
        self.assertEqual(len(dossier["decisions"][0]["objections"]), 2)

    def test_cannot_evaluate_without_snapshot_pinned(self) -> None:
        row = self.service.connection.execute(
            "SELECT snapshot_sha256 FROM plans WHERE plan_id='plan-balanced'").fetchone()
        self.assertEqual(row["snapshot_sha256"], self.digest)


class ActualVarianceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = make_service()
        self.digest = snapshot_v1(self.service)["content_sha256"]
        balanced_plan(self.service, self.digest)
        approve(self.service)

    def _actual(self, year: int, **overrides: object) -> dict:
        payload = {
            "plan_id": "plan-balanced", "period_year": year,
            "production_10kt": "60", "water_cut_percent": "80",
            "downtime_days": "0", "capex_spent_wan_yuan": "162000",
            "price_usd_bbl": "75",
        }
        payload.update(overrides)
        return self.service.record_actual("res-1", payload)

    def test_actual_only_for_approved_plan(self) -> None:
        self.service.create_plan("res-1", plan_payload(
            "plan-d", self.digest, name="d", groups=["wg-north-ridge"],
            capex_items=["capex-drilling-north"], production=["42", "56", "48", "40", "32"]))
        with self.assertRaisesRegex(Exception, "已批准"):
            self.service.record_actual("res-1", {
                "plan_id": "plan-d", "period_year": 1,
                "production_10kt": "42", "water_cut_percent": "80",
                "downtime_days": "0", "capex_spent_wan_yuan": "105840",
                "price_usd_bbl": "75"})

    def test_actual_is_append_only(self) -> None:
        self._actual(1)
        with self.assertRaisesRegex(Exception, "只能追加"):
            self._actual(1, production_10kt="61")

    def test_actual_does_not_mutate_decision_or_snapshot(self) -> None:
        before = self.service.dossier("aud-1", "plan-balanced")
        self._actual(1, water_cut_percent="95")
        after = self.service.dossier("aud-1", "plan-balanced")
        self.assertEqual(
            before["decisions"][0]["conclusion"], after["decisions"][0]["conclusion"])
        self.assertEqual(before["snapshot"]["content_sha256"], after["snapshot"]["content_sha256"])
        self.assertEqual(
            before["plan"]["evaluation"]["yearly_base"][0]["water_cut_percent"],
            after["plan"]["evaluation"]["yearly_base"][0]["water_cut_percent"])

    def test_variance_flags_beyond_tolerance(self) -> None:
        within = self._actual(1, production_10kt="60.5")
        self.assertFalse(within["variance"]["beyond_tolerance"])
        beyond = self._actual(2, production_10kt="70", water_cut_percent="88.6",
                              downtime_days="78", capex_spent_wan_yuan="55000",
                              price_usd_bbl="61")
        self.assertTrue(beyond["variance"]["beyond_tolerance"])
        self.assertTrue(beyond["variance"]["requires_new_scenario"])

    def test_period_year_must_be_within_horizon(self) -> None:
        with self.assertRaisesRegex(Exception, "period_year"):
            self._actual(6)


class DossierAndAuditTests(unittest.TestCase):
    def test_dossier_traces_all_evidence(self) -> None:
        service = make_service()
        digest = snapshot_v1(service)["content_sha256"]
        balanced_plan(service, digest)
        approve(service)
        dossier = service.dossier("dm-1", "plan-balanced")
        self.assertEqual(dossier["snapshot"]["content_sha256"], digest)
        self.assertEqual(dossier["snapshot"]["content"]["reserves"]["revision_id"],
                         "reserves-2026Q3")
        self.assertEqual(dossier["plan"]["evaluation_version"], EVALUATION_VERSION)
        self.assertIn("sensitivity", dossier["plan"]["evaluation"])
        self.assertIn("breakeven_price_usd_bbl", dossier["plan"]["evaluation"]["sensitivity"])
        self.assertTrue(dossier["events"])

    def test_audit_chain_valid(self) -> None:
        service = make_service()
        snapshot_v1(service)
        chain = service.audit_chain("aud-1")
        self.assertTrue(chain["valid"])
        self.assertGreaterEqual(chain["event_count"], 1)

    def test_auditor_cannot_make_decisions(self) -> None:
        service = make_service()
        digest = snapshot_v1(service)["content_sha256"]
        balanced_plan(service, digest)
        with self.assertRaisesRegex(Exception, "无权"):
            service.decide("aud-1", "plan-balanced", "approved", "x", expected_revision=1)


if __name__ == "__main__":
    unittest.main()
