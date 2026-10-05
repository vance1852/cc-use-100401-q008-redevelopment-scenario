"""二次开发情景治理的领域用例。

治理不变量：
- 输入快照内容寻址、不可修改，七类输入由油藏/工程/财务三方按小节背书；
- 方案只能引用确定的快照摘要，评价结果确定性可复现并固化算法版本；
- 投决不可改写，只能由更新的投决声明取代；每个油田同时只有一个生效批准；
- 实绩只能追加，产出偏差报告，不能回写方案、快照或旧投决。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .clock import SystemClock, isoformat
from .contracts import (
    SECTION_ATTESTATION_ROLES,
    ActualPerformance,
    DevelopmentPlan,
    InputSnapshot,
    ValidationError,
)
from .economics import EVALUATION_VERSION, evaluate
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, content_digest
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "reservoir": {
        "snapshot.write", "plan.write", "plan.evaluate", "actual.write",
        "objection.write", "report.read",
    },
    "facility": {
        "snapshot.write", "plan.write", "plan.evaluate", "actual.write",
        "objection.write", "report.read",
    },
    "finance": {
        "snapshot.write", "plan.write", "plan.evaluate", "actual.write",
        "objection.write", "report.read",
    },
    "decision_maker": {"decision.write", "objection.write", "report.read"},
    "auditor": {"report.read", "audit.read"},
}

VARIANCE_TOLERANCE_PERCENT = Decimal("10")
_QUANT = Decimal("0.01")
_DAYS_QUANT = Decimal("0.1")


def _q(value: Decimal) -> str:
    return format(value.quantize(_QUANT), "f")


class GovernanceService:
    """在单个 SQLite 连接上提供全部治理操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return isoformat(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT user_id, display_name, role, active FROM gov_users WHERE user_id=?",
            (user_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"用户不存在: {user_id}")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self, entity_type: str, entity_id: str, event_type: str,
        actor_id: str, payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type, entity_id, event_type, actor_id,
                canonical_json(payload), previous_hash, event_hash, body["created_at"],
            ),
        )

    # ------------------------------------------------------------------ 用户

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed(f"未知角色: {role}")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO gov_users(user_id,display_name,role) VALUES(?,?,?)",
                    (user_id.strip(), display_name.strip(), role),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"用户已存在: {user_id}") from exc
        return {"user_id": user_id.strip(), "role": role}

    # -------------------------------------------------------------- 输入快照

    def register_snapshot(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        actor = self._require(actor_id, "snapshot.write")
        try:
            snapshot = InputSnapshot.from_dict(raw)
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        for section, attester in snapshot.attestations.items():
            required_role = SECTION_ATTESTATION_ROLES[section]
            user = self._user(attester)
            if user["role"] != required_role:
                raise Forbidden(
                    f"快照小节 {section} 必须由 {required_role} 角色背书，"
                    f"当前为 {user['role']}")
        if self._user(actor_id)["role"] not in {
            self._user(attester)["role"] for attester in snapshot.attestations.values()
        }:
            raise Forbidden("提交人所在专业团队必须至少背书一个快照小节")
        text = canonical_json(raw)
        digest = content_digest([raw])
        with transaction(self.connection, immediate=True):
            latest = self.connection.execute(
                "SELECT coalesce(max(revision), 0) AS revision FROM input_snapshots WHERE snapshot_id=?",
                (snapshot.snapshot_id,),
            ).fetchone()
            revision = int(latest["revision"]) + 1
            try:
                self.connection.execute(
                    "INSERT INTO input_snapshots(snapshot_id,revision,asset_id,title,canonical_json,"
                    "content_sha256,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        snapshot.snapshot_id, revision, snapshot.asset_id, snapshot.title,
                        text, digest, actor_id, self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("快照编号版本或内容摘要冲突") from exc
            self._audit("snapshot", f"{snapshot.snapshot_id}@{revision}",
                        "snapshot.registered", actor_id,
                        {"asset_id": snapshot.asset_id, "sha256": digest,
                         "attestations": dict(snapshot.attestations)})
        return {
            "snapshot_id": snapshot.snapshot_id,
            "revision": revision,
            "asset_id": snapshot.asset_id,
            "content_sha256": digest,
            "state": "immutable",
        }

    def _snapshot_row(self, digest: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM input_snapshots WHERE content_sha256=?", (digest,)
        ).fetchone()
        if row is None:
            raise NotFound(f"输入快照版本不存在: {digest}")
        return row

    def _load_snapshot(self, digest: str) -> InputSnapshot:
        row = self._snapshot_row(digest)
        return InputSnapshot.from_dict(json.loads(row["canonical_json"]))

    # ------------------------------------------------------------------ 方案

    def create_plan(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "plan.write")
        digest_text = str(raw.get("snapshot_sha256", "")).strip().lower()
        snapshot = self._load_snapshot(digest_text)
        try:
            plan = DevelopmentPlan.from_dict(raw, snapshot)
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        text = canonical_json(raw)
        digest = content_digest([raw])
        with transaction(self.connection, immediate=True):
            try:
                self.connection.execute(
                    "INSERT INTO plans(plan_id,asset_id,name,snapshot_sha256,parent_plan_id,"
                    "canonical_json,content_sha256,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        plan.plan_id, plan.asset_id, plan.name, digest_text,
                        plan.parent_plan_id, text, digest, actor_id, self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("方案编号或内容摘要冲突，或父方案不存在") from exc
            self._audit("plan", plan.plan_id, "plan.created", actor_id,
                        {"snapshot_sha256": digest_text, "parent_plan_id": plan.parent_plan_id})
        return self.get_plan(plan.plan_id)

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFound("方案不存在")
        result = dict(row)
        result["evaluation"] = None if row["evaluation_json"] is None else json.loads(row["evaluation_json"])
        return result

    def evaluate_plan(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        self._require(actor_id, "plan.evaluate")
        row = self.connection.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFound("方案不存在")
        if row["evaluation_json"] is not None:
            return {
                "plan_id": plan_id,
                "evaluation_version": row["evaluation_version"],
                "result": json.loads(row["evaluation_json"]),
                "replayed": True,
            }
        snapshot = self._load_snapshot(row["snapshot_sha256"])
        plan = DevelopmentPlan.from_dict(json.loads(row["canonical_json"]), snapshot)
        result = evaluate(plan, snapshot)
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE plans SET state='evaluated',evaluation_json=?,evaluation_version=?,"
                "evaluated_by=?,evaluated_at=? WHERE plan_id=? AND evaluation_json IS NULL",
                (canonical_json(result), EVALUATION_VERSION, actor_id, self._now(), plan_id),
            )
            self._audit("plan", plan_id, "plan.evaluated", actor_id,
                        {"evaluation_version": EVALUATION_VERSION})
        return {"plan_id": plan_id, "evaluation_version": EVALUATION_VERSION,
                "result": result, "replayed": False}

    def compare_plans(self, actor_id: str, plan_ids: Sequence[str]) -> dict[str, Any]:
        """并排比较方案的增产、风险、回收期与敏感性；方案内容不可变。"""

        self._require(actor_id, "report.read")
        if not plan_ids or len(plan_ids) > 20:
            raise ValidationFailed("至少比较 1 个、至多 20 个方案")
        rows: list[dict[str, Any]] = []
        for plan_id in dict.fromkeys(plan_ids):
            row = self.connection.execute(
                "SELECT * FROM plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"方案不存在: {plan_id}")
            if row["evaluation_json"] is None:
                raise InvalidState(f"方案尚未评价，无法比较: {plan_id}")
            result = json.loads(row["evaluation_json"])
            base = result["price_scenarios"]["base"]
            rows.append({
                "plan_id": plan_id,
                "name": row["name"],
                "state": row["state"],
                "snapshot_sha256": row["snapshot_sha256"],
                "evaluation_version": row["evaluation_version"],
                "incremental_oil_10kt": result["totals"]["effective_incremental_oil_10kt"],
                "peak_year_oil_10kt": result["totals"]["peak_year_oil_10kt"],
                "outage_loss_oil_10kt": result["totals"]["outage_loss_oil_10kt"],
                "risk_level": result["totals"]["risk_level"],
                "risk_score": result["totals"]["risk_score"],
                "npv_low_wan_yuan": result["price_scenarios"]["low"]["npv_wan_yuan"],
                "npv_base_wan_yuan": base["npv_wan_yuan"],
                "npv_high_wan_yuan": result["price_scenarios"]["high"]["npv_wan_yuan"],
                "payback_discounted_years": base["payback_discounted_years"],
                "irr_percent": base["irr_percent"],
                "breakeven_price_usd_bbl": result["sensitivity"]["breakeven_price_usd_bbl"],
                "npv_band_minus_wan_yuan": result["sensitivity"]["npv_band_minus_wan_yuan"],
                "npv_band_plus_wan_yuan": result["sensitivity"]["npv_band_plus_wan_yuan"],
            })
        ranked = sorted(
            rows,
            key=lambda item: Decimal(item["npv_base_wan_yuan"]),
            reverse=True,
        )
        return {"count": len(rows), "plans": rows, "ranking_by_base_npv": [item["plan_id"] for item in ranked]}

    # ------------------------------------------------------------------ 投决

    def decide(
        self,
        actor_id: str,
        plan_id: str,
        outcome: str,
        conclusion: str,
        expected_revision: int,
        objections: Sequence[Mapping[str, Any]] | None = None,
        expected_active_decision_id: int | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "decision.write")
        if outcome not in {"approved", "rejected"}:
            raise ValidationFailed("投决结论必须是 approved 或 rejected")
        if not isinstance(conclusion, str) or not conclusion.strip():
            raise ValidationFailed("投决结论说明不能为空")
        plan = self.connection.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if plan is None:
            raise NotFound("方案不存在")
        if plan["revision"] != expected_revision:
            raise Conflict("方案修订版本已变化，请基于最新版本重新提交")
        if plan["evaluation_json"] is None:
            raise InvalidState("方案尚未完成评价，不能投决")
        if plan["state"] in {"approved", "rejected"}:
            raise InvalidState("方案已经形成投决，投决不可改写；请新建方案并引用新快照")
        parsed_objections = self._parse_objections(objections)
        with transaction(self.connection, immediate=True):
            previous_active = None
            if outcome == "approved":
                previous_active = self.connection.execute(
                    "SELECT decision_id FROM investment_decisions "
                    "WHERE asset_id=? AND outcome='approved' AND superseded_by IS NULL",
                    (plan["asset_id"],),
                ).fetchone()
                active_id = None if previous_active is None else previous_active["decision_id"]
                if active_id != expected_active_decision_id:
                    raise Conflict(
                        "生效投决已变化：请先审阅当前有效修订，"
                        "并在 expected_active_decision_id 中显式引用要取代的投决")
            # 新批准先以 rejected 落库（不占用生效批准唯一索引），随后挂接取代链、
            # 再翻转为 approved；全程在单个 IMMEDIATE 事务内，其他连接看不到中间态，
            # 部分唯一索引仍是并发批准的最后防线。
            try:
                cursor = self.connection.execute(
                    "INSERT INTO investment_decisions(asset_id,plan_id,plan_revision,snapshot_sha256,"
                    "outcome,conclusion,decided_by,decided_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        plan["asset_id"], plan_id, plan["revision"], plan["snapshot_sha256"],
                        "rejected" if outcome == "approved" else outcome,
                        conclusion.strip(), actor_id, self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("该方案修订已经存在投决，或存在并发批准") from exc
            decision_id = int(cursor.lastrowid)
            if outcome == "approved":
                if previous_active is not None:
                    self.connection.execute(
                        "UPDATE investment_decisions SET superseded_by=?,superseded_at=? "
                        "WHERE decision_id=?",
                        (decision_id, self._now(), previous_active["decision_id"]),
                    )
                    self.connection.execute(
                        "UPDATE plans SET state='superseded' WHERE plan_id="
                        "(SELECT plan_id FROM investment_decisions WHERE decision_id=?)",
                        (previous_active["decision_id"],),
                    )
                self.connection.execute(
                    "UPDATE investment_decisions SET outcome='approved' WHERE decision_id=?",
                    (decision_id,),
                )
            self.connection.execute(
                "UPDATE plans SET state=? WHERE plan_id=?", (outcome, plan_id)
            )
            for item in parsed_objections:
                self.connection.execute(
                    "INSERT INTO decision_objections(decision_id,raised_by,role,content,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (decision_id, item["raised_by"], item["role"], item["content"], self._now()),
                )
            self._audit(
                "decision", str(decision_id), f"decision.{outcome}", actor_id,
                {
                    "plan_id": plan_id,
                    "plan_revision": plan["revision"],
                    "snapshot_sha256": plan["snapshot_sha256"],
                    "evaluation_version": plan["evaluation_version"],
                    "supersedes": None if previous_active is None
                    else previous_active["decision_id"],
                    "objection_count": len(parsed_objections),
                },
            )
        return {
            "decision_id": decision_id,
            "plan_id": plan_id,
            "outcome": outcome,
            "snapshot_sha256": plan["snapshot_sha256"],
            "superseded_decision_id": None if previous_active is None
            else previous_active["decision_id"],
            "objections_recorded": len(parsed_objections),
        }

    def _parse_objections(
        self, objections: Sequence[Mapping[str, Any]] | None
    ) -> list[dict[str, str]]:
        items = objections or []
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            raise ValidationFailed("反对意见必须是数组")
        parsed: list[dict[str, str]] = []
        for index, raw in enumerate(items):
            if not isinstance(raw, Mapping):
                raise ValidationFailed(f"objections[{index}] 必须是对象")
            raised_by = str(raw.get("raised_by", "")).strip()
            content = str(raw.get("content", "")).strip()
            if not raised_by or not content:
                raise ValidationFailed(f"objections[{index}] 必须包含 raised_by 和 content")
            user = self._user(raised_by)
            if user["role"] == "auditor":
                raise Forbidden("审计角色不登记反对意见，请通过审计线索提出")
            parsed.append({"raised_by": raised_by, "role": user["role"], "content": content})
        return parsed

    def add_objection(self, actor_id: str, plan_id: str, content: str) -> dict[str, Any]:
        """对方案当前投决补充反对意见；只追加证据，不改变投决本身。"""

        actor = self._require(actor_id, "objection.write")
        if not isinstance(content, str) or not content.strip():
            raise ValidationFailed("反对意见内容不能为空")
        decision = self.connection.execute(
            "SELECT decision_id FROM investment_decisions WHERE plan_id=? "
            "ORDER BY decision_id DESC LIMIT 1",
            (plan_id,),
        ).fetchone()
        if decision is None:
            raise NotFound("方案尚无投决记录")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO decision_objections(decision_id,raised_by,role,content,created_at) "
                "VALUES(?,?,?,?,?)",
                (decision["decision_id"], actor_id, actor["role"], content.strip(), self._now()),
            )
            self._audit("decision", str(decision["decision_id"]),
                        "objection.added", actor_id,
                        {"objection_id": cursor.lastrowid})
        return {"objection_id": int(cursor.lastrowid),
                "decision_id": decision["decision_id"], "state": "appended"}

    def active_decision(self, actor_id: str, asset_id: str) -> dict[str, Any] | None:
        self._require(actor_id, "report.read")
        row = self.connection.execute(
            "SELECT * FROM investment_decisions WHERE asset_id=? AND outcome='approved' "
            "AND superseded_by IS NULL",
            (asset_id,),
        ).fetchone()
        return None if row is None else dict(row)

    # ------------------------------------------------------------------ 实绩

    def record_actual(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "actual.write")
        plan_row = self.connection.execute(
            "SELECT * FROM plans WHERE plan_id=?", (str(raw.get("plan_id", "")),)
        ).fetchone()
        if plan_row is None:
            raise NotFound("方案不存在")
        snapshot = self._load_snapshot(plan_row["snapshot_sha256"])
        decision = self.connection.execute(
            "SELECT decision_id FROM investment_decisions WHERE plan_id=? AND outcome='approved'",
            (plan_row["plan_id"],),
        ).fetchone()
        if decision is None:
            raise InvalidState("只有已批准方案可以录入实绩")
        try:
            actual = ActualPerformance.from_dict(raw, snapshot.horizon_years)
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        text = canonical_json(raw)
        digest = content_digest([raw])
        variance = self._variance(plan_row, snapshot, actual)
        with transaction(self.connection, immediate=True):
            try:
                actual_cursor = self.connection.execute(
                    "INSERT INTO actual_performances(plan_id,period_year,canonical_json,"
                    "content_sha256,recorded_by,recorded_at) VALUES(?,?,?,?,?,?)",
                    (actual.plan_id, actual.period_year, text, digest, actor_id, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("该方案评价年已有实绩；实绩只能追加，不能改写") from exc
            actual_id = int(actual_cursor.lastrowid)
            variance_cursor = self.connection.execute(
                "INSERT INTO variance_reports(plan_id,period_year,actual_id,result_json,"
                "created_by,created_at) VALUES(?,?,?,?,?,?)",
                (actual.plan_id, actual.period_year, actual_id,
                 canonical_json(variance), actor_id, self._now()),
            )
            self._audit("actual", str(actual_id), "actual.recorded", actor_id,
                        {"plan_id": actual.plan_id, "period_year": actual.period_year,
                         "variance_id": variance_cursor.lastrowid,
                         "beyond_tolerance": variance["beyond_tolerance"]})
        return {"actual_id": actual_id,
                "variance_id": int(variance_cursor.lastrowid),
                "variance": variance,
                "note": "实绩仅生成偏差；如需调整，请引用新快照新建情景，旧投决保持不变"}

    @staticmethod
    def _year_boundaries(snapshot: InputSnapshot, period_year: int) -> tuple[date, date]:
        start = date.fromisoformat(snapshot.start_date)
        try:
            year_start = start.replace(year=start.year + period_year - 1)
            year_end = start.replace(year=start.year + period_year)
        except ValueError:
            year_start = start.replace(year=start.year + period_year - 1, day=28)
            year_end = start.replace(year=start.year + period_year, day=28)
        return year_start, year_end

    def _variance(
        self, plan_row: sqlite3.Row, snapshot: InputSnapshot, actual: ActualPerformance
    ) -> dict[str, Any]:
        evaluation = json.loads(plan_row["evaluation_json"])
        forecast = evaluation["yearly_base"][actual.period_year - 1]
        year_start, year_end = self._year_boundaries(snapshot, actual.period_year)
        days_in_year = Decimal((year_end - year_start).days)
        planned_downtime = (
            (Decimal(1) - Decimal(forecast["outage_factor"])) * days_in_year
        ).quantize(_DAYS_QUANT)

        def deviation(actual_value: Decimal, forecast_value: Decimal) -> dict[str, str | bool]:
            delta = actual_value - forecast_value
            deviation_percent: str | None = (
                None if forecast_value == 0
                else _q((delta / forecast_value * Decimal(100)).quantize(_QUANT))
            )
            beyond = (
                forecast_value != 0
                and abs(delta / forecast_value * Decimal(100)) > VARIANCE_TOLERANCE_PERCENT
            )
            return {
                "forecast": _q(forecast_value),
                "actual": _q(actual_value),
                "delta": _q(delta.quantize(_QUANT)),
                "deviation_percent": deviation_percent,
                "beyond_tolerance": beyond,
            }

        production = deviation(
            actual.production_10kt, Decimal(forecast["effective_oil_10kt"]))
        water_cut = deviation(actual.water_cut_percent, Decimal(forecast["water_cut_percent"]))
        downtime = deviation(actual.downtime_days, planned_downtime)
        capex = deviation(actual.capex_spent_wan_yuan, Decimal(forecast["capex_wan_yuan"]))
        price = deviation(actual.price_usd_bbl, Decimal(forecast["price_usd_bbl"]))
        metrics = {
            "production_10kt": production,
            "water_cut_percent": water_cut,
            "downtime_days": downtime,
            "capex_wan_yuan": capex,
            "price_usd_bbl": price,
        }
        beyond = any(item["beyond_tolerance"] for item in metrics.values())
        return {
            "plan_id": actual.plan_id,
            "period_year": actual.period_year,
            "evaluation_version": plan_row["evaluation_version"],
            "snapshot_sha256": plan_row["snapshot_sha256"],
            "tolerance_percent": _q(VARIANCE_TOLERANCE_PERCENT),
            "metrics": metrics,
            "beyond_tolerance": beyond,
            "requires_new_scenario": beyond,
        }

    # ------------------------------------------------------------------ 追溯

    def dossier(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        """从投决结论追溯全部证据、假设、评价与敏感性边界。"""

        self._require(actor_id, "report.read")
        plan_row = self.connection.execute(
            "SELECT * FROM plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if plan_row is None:
            raise NotFound("方案不存在")
        snapshot_row = self._snapshot_row(plan_row["snapshot_sha256"])
        snapshot_raw = json.loads(snapshot_row["canonical_json"])
        decisions = self.connection.execute(
            "SELECT * FROM investment_decisions WHERE plan_id=? ORDER BY decision_id",
            (plan_id,),
        ).fetchall()
        decision_rows = []
        for decision in decisions:
            objections = self.connection.execute(
                "SELECT objection_id,raised_by,role,content,created_at "
                "FROM decision_objections WHERE decision_id=? ORDER BY objection_id",
                (decision["decision_id"],),
            ).fetchall()
            item = dict(decision)
            item["objections"] = [dict(row) for row in objections]
            decision_rows.append(item)
        actual_rows = self.connection.execute(
            "SELECT a.actual_id,a.period_year,a.content_sha256,a.recorded_by,a.recorded_at,"
            "v.variance_id,v.result_json FROM actual_performances a "
            "JOIN variance_reports v ON v.actual_id=a.actual_id "
            "WHERE a.plan_id=? ORDER BY a.period_year",
            (plan_id,),
        ).fetchall()
        events = self.connection.execute(
            "SELECT event_type,actor_id,payload_json,created_at,previous_hash,event_hash "
            "FROM audit_events WHERE "
            "(entity_type='plan' AND entity_id=?) "
            "OR (entity_type='snapshot' AND entity_id=?) "
            "OR (entity_type='decision' AND entity_id IN "
            "(SELECT CAST(decision_id AS TEXT) FROM investment_decisions WHERE plan_id=?)) "
            "OR (entity_type='actual' AND entity_id IN "
            "(SELECT CAST(actual_id AS TEXT) FROM actual_performances WHERE plan_id=?)) "
            "ORDER BY event_id",
            (plan_id, f"{snapshot_row['snapshot_id']}@{snapshot_row['revision']}",
             plan_id, plan_id),
        ).fetchall()
        return {
            "plan": {
                **{key: plan_row[key] for key in plan_row.keys() if key != "canonical_json"},
                "definition": json.loads(plan_row["canonical_json"]),
                "evaluation": None if plan_row["evaluation_json"] is None
                else json.loads(plan_row["evaluation_json"]),
            },
            "snapshot": {
                "snapshot_id": snapshot_row["snapshot_id"],
                "revision": snapshot_row["revision"],
                "asset_id": snapshot_row["asset_id"],
                "title": snapshot_row["title"],
                "content_sha256": snapshot_row["content_sha256"],
                "created_by": snapshot_row["created_by"],
                "created_at": snapshot_row["created_at"],
                "content": snapshot_raw,
            },
            "decisions": decision_rows,
            "actuals": [
                {
                    "actual_id": row["actual_id"],
                    "period_year": row["period_year"],
                    "content_sha256": row["content_sha256"],
                    "recorded_by": row["recorded_by"],
                    "recorded_at": row["recorded_at"],
                    "variance_id": row["variance_id"],
                    "variance": json.loads(row["result_json"]),
                }
                for row in actual_rows
            ],
            "events": [
                dict(row) | {"payload": json.loads(row["payload_json"])} for row in events
            ],
        }

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(
                canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"event_count": len(rows), "valid": valid}
