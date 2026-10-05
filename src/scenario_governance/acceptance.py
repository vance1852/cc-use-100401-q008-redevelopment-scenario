"""二次开发情景治理完整流程的离线验收入口。"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from .jsonio import load_json
from .service import GovernanceService
from .storage import connect, inspect_schema


def run(workspace: Path) -> dict[str, object]:
    demo = load_json(workspace / "fixtures" / "demo_redevelopment_inputs.json")
    with tempfile.TemporaryDirectory(prefix="scenario-governance-") as temporary:
        database = Path(temporary) / "governance.sqlite3"
        connection = connect(database)
        try:
            service = GovernanceService(connection)
            for user_id, role in (
                ("reservoir-1", "reservoir_engineer"),
                ("facility-1", "facility_engineer"),
                ("econ-1", "economist"),
                ("ops-1", "operations"),
                ("dm-1", "decision_maker"),
                ("auditor-1", "auditor"),
            ):
                service.create_user(user_id, user_id, role)
            project = demo["project"]
            service.register_project(
                "dm-1", project["project_id"], project["field_name"], project["source_pattern"]
            )
            submitters = {
                "reserves_version": "reservoir-1",
                "well_group_response": "reservoir-1",
                "water_cut_forecast": "reservoir-1",
                "facility_bottleneck": "facility-1",
                "shutdown_window": "facility-1",
                "capex": "facility-1",
                "oil_price_assumption": "econ-1",
            }
            selections = {}
            for item in demo["contributions"]:
                service.submit_contribution(
                    submitters[item["category"]], project["project_id"],
                    item["category"], item["version"], item["payload"],
                )
                selections[item["category"]] = item["version"]
            snapshot = service.compose_snapshot(
                "reservoir-1", "snapshot-2026.1", project["project_id"], selections
            )
            evaluations = {}
            creators = {"infill-only": "reservoir-1", "infill-plus-upgrade": "facility-1"}
            for item in demo["scenarios"]:
                service.create_scenario(
                    creators[item["scenario_id"]], item["scenario_id"], project["project_id"],
                    snapshot["snapshot_id"], item["definition"],
                )
                evaluations[item["scenario_id"]] = service.evaluate_scenario("econ-1", item["scenario_id"])
            replay = service.evaluate_scenario("econ-1", "infill-plus-upgrade")
            comparison = service.compare_scenarios(
                "dm-1", project["project_id"], [row["scenario_id"] for row in demo["scenarios"]]
            )
            winner = comparison["scenarios"][0]
            decision = service.decide(
                "dm-1", project["project_id"], winner["scenario_id"], winner["evaluation_id"],
                "approved", "增产、回收期与敏感性边界满足二次开发立项要求",
                0,
                dissents=[{
                    "author_id": "reservoir-1",
                    "opinion": "B2 井组响应置信度仅 72%，建议先实施先导井组再全面铺开",
                }],
            )
            actual = service.record_actual(
                "ops-1", project["project_id"],
                demo["actual_year1"]["horizon_year"], demo["actual_year1"]["metrics"],
            )
            derived = service.derive_scenario(
                "reservoir-1", "infill-plus-upgrade-r2", actual["actual_id"],
                {
                    "name": "实绩修正版：井网调整 + 设施改造（第二批井组）",
                    "infill_wells": [
                        {"well_group": "A1", "count": 6},
                        {"well_group": "B2", "count": 8},
                        {"well_group": "C3", "count": 4},
                    ],
                    "facility_upgrade": {
                        "additional_liquid_kl_per_day": "3000",
                        "additional_water_treatment_kl_per_day": "2200",
                        "additional_oil_processing_kl_per_day": "400",
                    },
                    "horizon_years": 10,
                },
            )
            revised = service.decide(
                "dm-1", project["project_id"], winner["scenario_id"], winner["evaluation_id"],
                "approved", "维持原方案，补充反对意见响应与先导井组节奏",
                decision["decision_revision"],
            )
            trace = service.trace_decision("auditor-1", decision["decision_id"])
            chain = service.audit_chain("auditor-1")
            schema = inspect_schema(connection)
        finally:
            connection.close()
    if schema["missing_tables"] or schema["schema_version"] != "1":
        raise RuntimeError("SQLite 基础结构检查失败")
    if not chain["valid"]:
        raise RuntimeError("审计哈希链校验失败")
    if len(trace["contributions"]) != 7:
        raise RuntimeError("投决追溯缺少输入类别")
    if not trace["dissents"]:
        raise RuntimeError("投决追溯缺少反对意见")
    if trace["decision"]["status"] != "superseded":
        raise RuntimeError("旧投决应保留为被取代状态")
    return {
        "status": "ok",
        "project": project["project_id"],
        "snapshot_sha256": snapshot["content_sha256"],
        "winner": winner["scenario_id"],
        "winner_uplift_kl": winner["uplift_kl"],
        "winner_payback_years": winner["payback_years"],
        "evaluation_replayed": replay["replayed"],
        "decision_revision": revised["decision_revision"],
        "deviation_beyond_boundary": actual["deviation"]["variance"]["beyond_sensitivity_boundary"],
        "derived_scenario": derived["scenario_id"],
        "trace_contributions": len(trace["contributions"]),
        "trace_events": len(trace["events"]),
        "audit_events": chain["events"],
        "schema": schema,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行二次开发情景治理的离线自检")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    result = run(args.workspace.resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
