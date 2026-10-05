"""二次开发情景治理的离线验收。

在临时 SQLite 数据库中演示：三方背书的不可变快照、双方案比较、带反对意见的
版本化批准、实绩偏差触发新情景、新批准取代旧批准（同时只有一个生效）、
旧投决不可改写，以及从结论到全部证据/假设/敏感性边界的追溯。
"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .clock import FrozenClock
from .service import GovernanceService
from .storage import connect, inspect_schema


def snapshot_payload(
    *,
    snapshot_id: str = "liuhua-inputs",
    revision_note: str = "流花模式输入基线",
    price_base: str = "75",
    attesters: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    attesters = attesters or {
        "reserves": "res-1",
        "well_groups": "res-1",
        "water_cut": "res-1",
        "facility_constraints": "fac-1",
        "outage_windows": "fac-1",
        "capex": "fin-1",
        "oil_price": "fin-1",
    }
    return {
        "snapshot_id": snapshot_id,
        "asset_id": "liuhua-deepwater",
        "title": f"流花油田二次开发输入快照-{revision_note}",
        "horizon_years": 5,
        "start_date": "2027-01-01",
        "reserves": {
            "revision_id": "reserves-2026Q3",
            "basis": "proved_probable",
            "recoverable_oil_10kt": "1200.0000",
            "as_of_date": "2026-09-30",
            "content_sha256": "b" * 64,
            "note": "流花礁灰岩油藏复算储量",
        },
        "well_groups": [
            {
                "well_group_id": "wg-north-ridge",
                "name": "北部屋脊调整井组",
                "response_delay_months": 3,
                "incremental_oil_10kt_yearly": ["42", "56", "48", "40", "32"],
                "decline_rate_percent": "14",
                "confidence": "high",
            },
            {
                "well_group_id": "wg-south-flank",
                "name": "南翼侧钻井组",
                "response_delay_months": 9,
                "incremental_oil_10kt_yearly": ["18", "34", "40", "36", "30"],
                "decline_rate_percent": "11",
                "confidence": "medium",
            },
        ],
        "water_cut": {
            "initial_percent": "78",
            "yearly_percent": ["80", "83", "85", "87", "88"],
            "limit_percent": "94",
        },
        "facility_constraints": [
            {
                "facility_id": "fpso-liuhua",
                "name": "流花号 FPSO 处理系统",
                "system": "process-train",
                "capacity": "95000",
                "capacity_unit": "bbl/day",
                "peak_demand_percent": "112",
                "debottleneck_capex_item": "capex-fpso-retrofit",
            },
            {
                "facility_id": "subsea-manifold-a",
                "name": "水下管汇 A",
                "system": "subsea",
                "capacity": "120000",
                "capacity_unit": "bbl/day",
                "peak_demand_percent": "86",
                "debottleneck_capex_item": None,
            },
        ],
        "outage_windows": [
            {
                "window_id": "outage-fpso-2028",
                "facility_id": "fpso-liuhua",
                "reason": "工艺模块停产改造",
                "start_date": "2028-03-01",
                "end_date": "2028-04-15",
                "capacity_loss_percent": "100",
            }
        ],
        "capex": [
            {
                "item_id": "capex-drilling-north",
                "name": "北部屋脊 6 口调整井钻完井",
                "category": "drilling_completion",
                "year": 1,
                "amount_wan_yuan": "98000",
                "contingency_percent": "8",
            },
            {
                "item_id": "capex-drilling-south",
                "name": "南翼 4 口侧钻井钻完井",
                "category": "drilling_completion",
                "year": 1,
                "amount_wan_yuan": "52000",
                "contingency_percent": "10",
            },
            {
                "item_id": "capex-fpso-retrofit",
                "name": "FPSO 处理系统扩容改造",
                "category": "facility_retrofit",
                "year": 2,
                "amount_wan_yuan": "46000",
                "contingency_percent": "12",
            },
        ],
        "oil_price": {
            "market_index": "BRENT",
            "base_usd_bbl": price_base,
            "low_usd_bbl": "55",
            "high_usd_bbl": "95",
            "escalation_percent": "2",
            "exchange_rate_cny_usd": "7.10",
            "barrels_per_tonne": "7.35",
            "sensitivity_band_percent": "15",
        },
        "attestations": dict(attesters),
    }


def plan_payload(
    plan_id: str,
    snapshot_sha256: str,
    *,
    name: str,
    groups: list[str],
    capex_items: list[str],
    production: list[str],
    overrun: str = "5",
    risk_level: str = "medium",
    risk_score: str = "42",
    factors: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "plan_id": plan_id,
        "asset_id": "liuhua-deepwater",
        "name": name,
        "snapshot_sha256": snapshot_sha256,
        "parent_plan_id": None,
        "selected_well_groups": groups,
        "selected_capex_items": capex_items,
        "incremental_oil_10kt_yearly": production,
        "opex_usd_bbl": "21.5",
        "discount_rate_percent": "9",
        "capex_overrun_percent": overrun,
        "risk": {
            "risk_level": risk_level,
            "risk_score": risk_score,
            "factors": factors or ["改造窗口与台风季叠加", "高含水提升举升成本"],
        },
    }


def _seed_users(service: GovernanceService) -> None:
    service.create_user("res-1", "油藏负责人", "reservoir")
    service.create_user("fac-1", "工程负责人", "facility")
    service.create_user("fin-1", "财务负责人", "finance")
    service.create_user("dm-1", "投资决策人", "decision_maker")
    service.create_user("aud-1", "审计人员", "auditor")


def run(workspace: Path) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="scenario-governance-") as temporary:
        database = Path(temporary) / "governance.sqlite3"
        connection = connect(database)
        try:
            service = GovernanceService(
                connection, FrozenClock(datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)))
            _seed_users(service)

            # 1) 三方背书的不可变输入快照 v1
            snapshot_v1 = service.register_snapshot("res-1", snapshot_payload())
            digest_v1 = snapshot_v1["content_sha256"]

            # 2) 两个可比方案：北部激进 vs 南北稳健，引用同一确定快照版本
            service.create_plan("res-1", plan_payload(
                "plan-aggressive", digest_v1,
                name="北部屋脊优先（激进）",
                groups=["wg-north-ridge"],
                capex_items=["capex-drilling-north", "capex-fpso-retrofit"],
                production=["42", "56", "48", "40", "32"],
                overrun="8", risk_level="high", risk_score="61",
                factors=["FPSO 瓶颈改造延期风险", "单井组稳产不确定性"],
            ))
            service.create_plan("res-1", plan_payload(
                "plan-balanced", digest_v1,
                name="南北联合调整（稳健）",
                groups=["wg-north-ridge", "wg-south-flank"],
                capex_items=["capex-drilling-north", "capex-drilling-south", "capex-fpso-retrofit"],
                production=["60", "90", "88", "76", "62"],
                overrun="5", risk_level="medium", risk_score="42",
            ))
            evaluation_a = service.evaluate_plan("fin-1", "plan-aggressive")
            evaluation_b = service.evaluate_plan("fin-1", "plan-balanced")
            comparison = service.compare_plans(
                "dm-1", ["plan-aggressive", "plan-balanced"])

            # 评价必须可逐字节复现
            replayed = service.evaluate_plan("fin-1", "plan-balanced")
            if not replayed["replayed"] or replayed["result"] != evaluation_b["result"]:
                raise RuntimeError("方案评价不可复现")

            # 3) 批准稳健方案，引用确定版本，保留反对意见
            decision_b = service.decide(
                "dm-1", "plan-balanced", "approved",
                "稳健方案 NPV 与回收期更优，风险评分可控，同意按 2027 年实施",
                expected_revision=1,
                objections=[
                    {"raised_by": "fac-1",
                     "content": "FPSO 改造窗口仅 46 天，建议预留海上安装备用窗口"},
                    {"raised_by": "fin-1",
                     "content": "若油价跌破 60 美元，NPV 对超支假设敏感，建议设触发复盘"},
                ],
            )

            # 4) 旧投决不可改写
            rewrite_blocked = False
            try:
                service.decide(
                    "dm-1", "plan-balanced", "rejected", "试图改写旧投决",
                    expected_revision=1)
            except Exception:
                rewrite_blocked = True
            if not rewrite_blocked:
                raise RuntimeError("旧投决被改写，治理不变量被破坏")

            # 5) 实绩：第一年在容差内，第二年含水与停产偏差超容差
            actual_y1 = service.record_actual("res-1", {
                "plan_id": "plan-balanced", "period_year": 1,
                "production_10kt": "61.2", "water_cut_percent": "80.5",
                "downtime_days": "0", "capex_spent_wan_yuan": "162000",
                "price_usd_bbl": "74.2",
            })
            actual_y2 = service.record_actual("fac-1", {
                "plan_id": "plan-balanced", "period_year": 2,
                "production_10kt": "78.0", "water_cut_percent": "88.6",
                "downtime_days": "78", "capex_spent_wan_yuan": "55000",
                "price_usd_bbl": "61.0",
            })

            # 实绩不能改写
            actual_rewrite_blocked = False
            try:
                service.record_actual("res-1", {
                    "plan_id": "plan-balanced", "period_year": 1,
                    "production_10kt": "99", "water_cut_percent": "80",
                    "downtime_days": "0", "capex_spent_wan_yuan": "162000",
                    "price_usd_bbl": "74.2",
                })
            except Exception:
                actual_rewrite_blocked = True
            if not actual_rewrite_blocked:
                raise RuntimeError("实绩被改写，治理不变量被破坏")

            # 6) 偏差触发新情景：引用新快照 v2（更新油价/含水/设施假设）
            service.clock.advance(days=400)
            snapshot_v2 = service.register_snapshot("res-1", snapshot_payload(
                revision_note="实绩偏差后修订", price_base="66"))
            digest_v2 = snapshot_v2["content_sha256"]
            if digest_v2 == digest_v1:
                raise RuntimeError("不同输入产生了相同快照摘要")
            service.create_plan("res-1", plan_payload(
                "plan-revised", digest_v2,
                name="南北联合调整（偏差修订）",
                groups=["wg-north-ridge", "wg-south-flank"],
                capex_items=["capex-drilling-north", "capex-drilling-south", "capex-fpso-retrofit"],
                production=["58", "82", "84", "74", "60"],
                overrun="9", risk_level="medium", risk_score="47",
                factors=["第二年含水上升快于预测", "FPSO 改造实际停产更长"],
            ))
            service.evaluate_plan("fin-1", "plan-revised")
            decision_v2 = service.decide(
                "dm-1", "plan-revised", "approved",
                "依据第二年偏差与新油价假设修订，取代原批准",
                expected_revision=1,
                expected_active_decision_id=decision_b["decision_id"])

            # 并发批准只能有一个有效修订
            active = service.active_decision("aud-1", "liuhua-deepwater")
            if active is None or active["plan_id"] != "plan-revised":
                raise RuntimeError("生效投决不是最新批准")
            active_count = connection.execute(
                "SELECT count(*) AS c FROM investment_decisions "
                "WHERE asset_id='liuhua-deepwater' AND outcome='approved' AND superseded_by IS NULL"
            ).fetchone()["c"]
            if active_count != 1:
                raise RuntimeError(f"生效批准数量应为 1，实际 {active_count}")

            # 7) 从结论追溯全部证据、假设、敏感性边界与反对意见
            dossier = service.dossier("aud-1", "plan-balanced")
            chain = service.audit_chain("aud-1")
            schema = inspect_schema(connection)
        finally:
            connection.close()

    if schema["missing_tables"] or schema["schema_version"] != "1":
        raise RuntimeError("SQLite 结构检查失败")
    if not chain["valid"]:
        raise RuntimeError("审计哈希链校验失败")
    if len(dossier["decisions"]) != 1 or not dossier["decisions"][0]["objections"]:
        raise RuntimeError("追溯档案缺少投决或反对意见")
    if dossier["snapshot"]["content_sha256"] != digest_v1:
        raise RuntimeError("追溯档案引用了错误的快照版本")
    sensitivity = evaluation_b["result"]["sensitivity"]
    return {
        "status": "ok",
        "snapshot_v1": digest_v1,
        "snapshot_v2": digest_v2,
        "evaluation_version": evaluation_b["evaluation_version"],
        "comparison_ranking": comparison["ranking_by_base_npv"],
        "npv_base_balanced_wan_yuan":
            evaluation_b["result"]["price_scenarios"]["base"]["npv_wan_yuan"],
        "payback_balanced_years":
            evaluation_b["result"]["price_scenarios"]["base"]["payback_discounted_years"],
        "breakeven_price_usd_bbl": sensitivity["breakeven_price_usd_bbl"],
        "sensitivity_band": [
            sensitivity["npv_band_minus_wan_yuan"], sensitivity["npv_band_plus_wan_yuan"]],
        "first_decision": decision_b["decision_id"],
        "revised_decision": decision_v2["decision_id"],
        "superseded": decision_v2["superseded_decision_id"],
        "variance_y1_beyond": actual_y1["variance"]["beyond_tolerance"],
        "variance_y2_beyond": actual_y2["variance"]["beyond_tolerance"],
        "old_decision_rewrite_blocked": rewrite_blocked,
        "actual_rewrite_blocked": actual_rewrite_blocked,
        "objections_retained": len(dossier["decisions"][0]["objections"]),
        "active_plan": active["plan_id"],
        "audit_events": chain["event_count"],
        "audit_chain_valid": chain["valid"],
        "schema": schema,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行二次开发情景治理离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    result = run(args.workspace.resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
