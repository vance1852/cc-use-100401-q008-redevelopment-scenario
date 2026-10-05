"""二次开发情景治理的领域用例。

治理主线：七类输入贡献 → 不可变输入快照 → 方案与确定性评价 → 引用确定
版本的投决（保留反对意见、并发只有一个有效修订）→ 实绩只生成偏差与新
情景，旧投决内容永不改写，结论可追溯到全部证据、假设与敏感性边界。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .canonical import canonical_json, content_digest
from .clock import SystemClock, isoformat
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .evaluation import ALGORITHM_VERSION, EvalInputs, deviation_variance, evaluate
from .models import (
    CATEGORIES,
    CATEGORY_LABELS,
    ActualMetrics,
    CapexAssumption,
    FacilityBottleneck,
    OilPriceAssumption,
    ReservesVersion,
    ScenarioDefinition,
    ShutdownWindow,
    WaterCutForecast,
    WellGroupResponse,
    parse_contribution,
    required_text,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "reservoir_engineer": {
        "input.submit.reservoir", "snapshot.compose", "scenario.write",
        "scenario.evaluate", "scenario.compare", "dissent.record",
    },
    "facility_engineer": {
        "input.submit.facility", "snapshot.compose", "scenario.write",
        "scenario.evaluate", "scenario.compare", "dissent.record",
    },
    "economist": {
        "input.submit.economy", "snapshot.compose", "scenario.write",
        "scenario.evaluate", "scenario.compare", "dissent.record",
    },
    "operations": {"actual.record"},
    "decision_maker": {
        "project.register", "project.close", "decision.write", "scenario.compare",
        "dissent.record", "trace.read",
    },
    "auditor": {"scenario.compare", "trace.read", "audit.read"},
}

CATEGORY_PERMISSIONS = {
    "reserves_version": "input.submit.reservoir",
    "well_group_response": "input.submit.reservoir",
    "water_cut_forecast": "input.submit.reservoir",
    "facility_bottleneck": "input.submit.facility",
    "shutdown_window": "input.submit.facility",
    "capex": "input.submit.facility",
    "oil_price_assumption": "input.submit.economy",
}


class GovernanceService:
    """在单个 SQLite 连接上提供全部情景治理操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return isoformat(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT user_id, display_name, role, active FROM users WHERE user_id=?", (user_id,)
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
        self, entity_type: str, entity_id: str, event_type: str, actor_id: str, payload: Mapping[str, Any]
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
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed(f"未知角色: {role}")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"用户已存在: {user_id}") from exc
        return {"user_id": user_id.strip(), "role": role}

    def register_project(
        self, actor_id: str, project_id: str, field_name: str, source_pattern: str
    ) -> dict[str, Any]:
        """登记二次开发项目，记录复制来源模式（如流花模式）。"""

        self._require(actor_id, "project.register")
        field_name = required_text(field_name, "field_name", 128)
        source_pattern = required_text(source_pattern, "source_pattern", 64)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO redevelopment_projects(project_id,field_name,source_pattern,created_by,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (project_id, field_name, source_pattern, actor_id, self._now()),
                )
                self._audit(
                    "project", project_id, "project.registered", actor_id,
                    {"field_name": field_name, "source_pattern": source_pattern},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"二次开发项目已存在: {project_id}") from exc
        return {"project_id": project_id, "field_name": field_name, "source_pattern": source_pattern}

    def _project(self, project_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM redevelopment_projects WHERE project_id=?", (project_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"二次开发项目不存在: {project_id}")
        return row

    def close_project(self, actor_id: str, project_id: str) -> dict[str, Any]:
        """关闭项目：不再接受新的输入贡献与投决，历史记录保持可追溯。"""

        self._require(actor_id, "project.close")
        self._project(project_id)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE redevelopment_projects SET state='closed' WHERE project_id=? AND state='open'",
                (project_id,),
            )
            if cursor.rowcount != 1:
                raise InvalidState("项目已经关闭")
            self._audit("project", project_id, "project.closed", actor_id, {})
        return {"project_id": project_id, "state": "closed"}

    def submit_contribution(
        self, actor_id: str, project_id: str, category: str, version: str, payload: object
    ) -> dict[str, Any]:
        """团队提交一类输入假设；内容规范化后计算摘要，提交即不可变。"""

        if category not in CATEGORY_PERMISSIONS:
            raise ValidationFailed(f"未知输入贡献类别: {category}")
        self._require(actor_id, CATEGORY_PERMISSIONS[category])
        project = self._project(project_id)
        if project["state"] != "open":
            raise InvalidState("项目已关闭，不能提交输入贡献")
        version = required_text(version, "version", 64)
        parsed = parse_contribution(category, payload)
        canonical = canonical_json(parsed.as_canonical())
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        contribution_id = f"{project_id}:{category}:{version}"
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO input_contributions(contribution_id,project_id,category,version,payload_json,"
                    "content_sha256,submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?)",
                    (contribution_id, project_id, category, version, canonical, digest, actor_id, self._now()),
                )
                self._audit(
                    "contribution", contribution_id, "contribution.submitted", actor_id,
                    {"category": category, "version": version, "content_sha256": digest},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("输入贡献编号、版本或内容摘要冲突") from exc
        return {
            "contribution_id": contribution_id,
            "category": category,
            "category_label": CATEGORY_LABELS[category],
            "version": version,
            "content_sha256": digest,
        }

    def compose_snapshot(
        self, actor_id: str, snapshot_id: str, project_id: str, selections: Mapping[str, str]
    ) -> dict[str, Any]:
        """把七类输入的确定版本组合为不可变输入快照。"""

        self._require(actor_id, "snapshot.compose")
        self._project(project_id)
        if not isinstance(selections, Mapping):
            raise ValidationFailed("selections 必须是对象")
        unknown = sorted(set(selections) - set(CATEGORIES))
        if unknown:
            raise ValidationFailed(f"快照包含未知输入类别: {','.join(unknown)}")
        missing = [category for category in CATEGORIES if category not in selections]
        if missing:
            labels = "、".join(CATEGORY_LABELS[category] for category in missing)
            raise ValidationFailed(f"输入快照缺少类别: {labels}")
        composition: dict[str, Any] = {}
        for category in CATEGORIES:
            version = required_text(selections[category], f"selections.{category}", 64)
            row = self.connection.execute(
                "SELECT contribution_id,version,content_sha256 FROM input_contributions "
                "WHERE project_id=? AND category=? AND version=?",
                (project_id, category, version),
            ).fetchone()
            if row is None:
                raise NotFound(f"输入贡献不存在: {CATEGORY_LABELS[category]}@{version}")
            composition[category] = {
                "contribution_id": row["contribution_id"],
                "version": row["version"],
                "content_sha256": row["content_sha256"],
            }
        digest = content_digest([composition])
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO input_snapshots(snapshot_id,project_id,composition_json,content_sha256,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (snapshot_id, project_id, canonical_json(composition), digest, actor_id, self._now()),
                )
                self._audit(
                    "snapshot", snapshot_id, "snapshot.composed", actor_id,
                    {"project_id": project_id, "content_sha256": digest},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("快照编号冲突或相同内容的快照已存在") from exc
        return {"snapshot_id": snapshot_id, "project_id": project_id, "content_sha256": digest}

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM input_snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"输入快照不存在: {snapshot_id}")
        return dict(row) | {"composition": json.loads(row["composition_json"])}

    def _snapshot_inputs(self, snapshot_row: sqlite3.Row) -> EvalInputs:
        composition = json.loads(snapshot_row["composition_json"])
        payloads: dict[str, Any] = {}
        for category, reference in composition.items():
            row = self.connection.execute(
                "SELECT payload_json FROM input_contributions WHERE contribution_id=?",
                (reference["contribution_id"],),
            ).fetchone()
            if row is None:
                raise InvalidState(f"快照引用的输入贡献缺失: {reference['contribution_id']}")
            payloads[category] = json.loads(row["payload_json"])
        return EvalInputs(
            reserves=ReservesVersion.from_dict(payloads["reserves_version"]),
            well_groups=WellGroupResponse.from_dict(payloads["well_group_response"]),
            water_cut=WaterCutForecast.from_dict(payloads["water_cut_forecast"]),
            facility=FacilityBottleneck.from_dict(payloads["facility_bottleneck"]),
            shutdown=ShutdownWindow.from_dict(payloads["shutdown_window"]),
            capex=CapexAssumption.from_dict(payloads["capex"]),
            oil_price=OilPriceAssumption.from_dict(payloads["oil_price_assumption"]),
        )

    def _check_definition_against_inputs(
        self, definition: ScenarioDefinition, inputs: EvalInputs
    ) -> None:
        groups = {group.well_group: group for group in inputs.well_groups.groups}
        for plan in definition.infill_wells:
            group = groups.get(plan.well_group)
            if group is None:
                raise ValidationFailed(f"方案引用了快照中不存在的井组: {plan.well_group}")
            if plan.count > group.max_infill_wells:
                raise ValidationFailed(
                    f"井组 {plan.well_group} 方案井数 {plan.count} 超出快照上限 {group.max_infill_wells}"
                )

    def _insert_scenario(
        self,
        actor_id: str,
        scenario_id: str,
        project_id: str,
        snapshot_row: sqlite3.Row,
        definition: ScenarioDefinition,
        derived_from_actual_id: int | None,
    ) -> dict[str, Any]:
        canonical = canonical_json(definition.as_canonical())
        digest = content_digest([{
            "definition": definition.as_canonical(),
            "snapshot_sha256": snapshot_row["content_sha256"],
        }])
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO scenarios(scenario_id,project_id,snapshot_id,snapshot_sha256,name,"
                    "definition_json,content_sha256,derived_from_actual_id,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        scenario_id, project_id, snapshot_row["snapshot_id"], snapshot_row["content_sha256"],
                        definition.name, canonical, digest, derived_from_actual_id, actor_id, self._now(),
                    ),
                )
                self._audit(
                    "scenario", scenario_id, "scenario.created", actor_id,
                    {
                        "snapshot_sha256": snapshot_row["content_sha256"],
                        "content_sha256": digest,
                        "derived_from_actual_id": derived_from_actual_id,
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("方案编号冲突或相同内容的方案已存在") from exc
        return {
            "scenario_id": scenario_id,
            "snapshot_id": snapshot_row["snapshot_id"],
            "snapshot_sha256": snapshot_row["content_sha256"],
            "content_sha256": digest,
        }

    def create_scenario(
        self,
        actor_id: str,
        scenario_id: str,
        project_id: str,
        snapshot_id: str,
        definition_raw: object,
    ) -> dict[str, Any]:
        """在指定输入快照上创建候选方案；方案创建即不可变。"""

        self._require(actor_id, "scenario.write")
        self._project(project_id)
        snapshot_row = self.connection.execute(
            "SELECT * FROM input_snapshots WHERE snapshot_id=? AND project_id=?",
            (snapshot_id, project_id),
        ).fetchone()
        if snapshot_row is None:
            raise NotFound(f"输入快照不存在: {snapshot_id}")
        definition = ScenarioDefinition.from_dict(
            definition_raw if isinstance(definition_raw, Mapping) else {}
        )
        inputs = self._snapshot_inputs(snapshot_row)
        self._check_definition_against_inputs(definition, inputs)
        return self._insert_scenario(actor_id, scenario_id, project_id, snapshot_row, definition, None)

    def get_scenario(self, scenario_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM scenarios WHERE scenario_id=?", (scenario_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"方案不存在: {scenario_id}")
        return dict(row) | {"definition": json.loads(row["definition_json"])}

    def evaluate_scenario(self, actor_id: str, scenario_id: str) -> dict[str, Any]:
        """确定性评价；相同输入重放直接返回已存结果。"""

        self._require(actor_id, "scenario.evaluate")
        scenario = self.connection.execute(
            "SELECT * FROM scenarios WHERE scenario_id=?", (scenario_id,)
        ).fetchone()
        if scenario is None:
            raise NotFound(f"方案不存在: {scenario_id}")
        snapshot_row = self.connection.execute(
            "SELECT * FROM input_snapshots WHERE snapshot_id=?", (scenario["snapshot_id"],)
        ).fetchone()
        inputs = self._snapshot_inputs(snapshot_row)
        definition = ScenarioDefinition.from_dict(json.loads(scenario["definition_json"]))
        input_digest = content_digest([{
            "algorithm_version": ALGORITHM_VERSION,
            "scenario_sha256": scenario["content_sha256"],
            "snapshot_sha256": scenario["snapshot_sha256"],
        }])
        existing = self.connection.execute(
            "SELECT evaluation_id,result_json FROM evaluations WHERE scenario_id=? AND input_sha256=?",
            (scenario_id, input_digest),
        ).fetchone()
        if existing is not None:
            return {
                "evaluation_id": existing["evaluation_id"],
                "input_sha256": input_digest,
                "result": json.loads(existing["result_json"]),
                "replayed": True,
            }
        result = evaluate(inputs, definition)
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO evaluations(scenario_id,snapshot_sha256,algorithm_version,input_sha256,"
                    "result_json,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        scenario_id, scenario["snapshot_sha256"], ALGORITHM_VERSION, input_digest,
                        canonical_json(result), actor_id, self._now(),
                    ),
                )
                evaluation_id = int(cursor.lastrowid)
                self._audit(
                    "scenario", scenario_id, "scenario.evaluated", actor_id,
                    {"evaluation_id": evaluation_id, "input_sha256": input_digest},
                )
        except sqlite3.IntegrityError:
            existing = self.connection.execute(
                "SELECT evaluation_id,result_json FROM evaluations WHERE scenario_id=? AND input_sha256=?",
                (scenario_id, input_digest),
            ).fetchone()
            if existing is None:
                raise
            return {
                "evaluation_id": existing["evaluation_id"],
                "input_sha256": input_digest,
                "result": json.loads(existing["result_json"]),
                "replayed": True,
            }
        return {
            "evaluation_id": evaluation_id,
            "input_sha256": input_digest,
            "result": result,
            "replayed": False,
        }

    def compare_scenarios(
        self, actor_id: str, project_id: str, scenario_ids: Iterable[str]
    ) -> dict[str, Any]:
        """并排比较候选方案的增产、风险与回收期。"""

        self._require(actor_id, "scenario.compare")
        self._project(project_id)
        if not isinstance(scenario_ids, (list, tuple)):
            raise ValidationFailed("scenario_ids 必须是数组")
        ids = list(dict.fromkeys(scenario_ids))
        if not ids:
            raise ValidationFailed("scenario_ids 不能为空")
        rows: list[dict[str, Any]] = []
        for scenario_id in ids:
            scenario = self.connection.execute(
                "SELECT * FROM scenarios WHERE scenario_id=? AND project_id=?",
                (scenario_id, project_id),
            ).fetchone()
            if scenario is None:
                raise NotFound(f"方案不存在: {scenario_id}")
            evaluation = self.connection.execute(
                "SELECT evaluation_id,result_json FROM evaluations WHERE scenario_id=? "
                "ORDER BY evaluation_id DESC LIMIT 1",
                (scenario_id,),
            ).fetchone()
            if evaluation is None:
                raise InvalidState(f"方案尚未评价，不能比较: {scenario_id}")
            result = json.loads(evaluation["result_json"])
            rows.append({
                "scenario_id": scenario_id,
                "name": scenario["name"],
                "evaluation_id": evaluation["evaluation_id"],
                "snapshot_sha256": scenario["snapshot_sha256"],
                "uplift_kl": result["uplift_kl"],
                "uplift_percent": result["uplift_percent"],
                "npv_m_cny": result["npv_m_cny"],
                "payback_years": result["payback_years"],
                "payback_beyond_horizon": result["payback_beyond_horizon"],
                "risk": {"score": result["risk"]["score"], "classification": result["risk"]["classification"]},
                "break_even_oil_price_cny_per_kl": result["sensitivity"]["break_even_oil_price_cny_per_kl"],
            })
        rows.sort(key=lambda row: (-Decimal(row["npv_m_cny"]), row["scenario_id"]))
        for rank, row in enumerate(rows, start=1):
            row["rank"] = rank
        return {"project_id": project_id, "scenarios": rows}

    def decide(
        self,
        actor_id: str,
        project_id: str,
        scenario_id: str,
        evaluation_id: int,
        decision: str,
        rationale: str,
        expected_decision_revision: int,
        dissents: Iterable[Mapping[str, str]] = (),
    ) -> dict[str, Any]:
        """形成投决：引用确定版本、保留反对意见、并发只有一个有效修订。"""

        self._require(actor_id, "decision.write")
        if decision not in {"approved", "rejected"}:
            raise ValidationFailed("投决结论必须是 approved 或 rejected")
        rationale = required_text(rationale, "rationale", 512)
        if expected_decision_revision < 0:
            raise ValidationFailed("expected_decision_revision 不能为负数")
        project = self._project(project_id)
        if project["state"] != "open":
            raise InvalidState("项目已关闭，不能形成投决")
        scenario = self.connection.execute(
            "SELECT * FROM scenarios WHERE scenario_id=? AND project_id=?",
            (scenario_id, project_id),
        ).fetchone()
        if scenario is None:
            raise NotFound(f"方案不存在: {scenario_id}")
        evaluation = self.connection.execute(
            "SELECT * FROM evaluations WHERE evaluation_id=? AND scenario_id=?",
            (evaluation_id, scenario_id),
        ).fetchone()
        if evaluation is None:
            raise NotFound("评价版本不存在或不属于该方案")
        if evaluation["snapshot_sha256"] != scenario["snapshot_sha256"]:
            raise Conflict("评价引用的快照版本与方案不一致")
        parsed_dissents: list[dict[str, str]] = []
        for item in dissents:
            if not isinstance(item, Mapping):
                raise ValidationFailed("dissents 元素必须是对象")
            author = required_text(item.get("author_id"), "dissents.author_id", 64)
            self._user(author)
            parsed_dissents.append({
                "author_id": author,
                "opinion": required_text(item.get("opinion"), "dissents.opinion", 512),
            })
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                current = self.connection.execute(
                    "SELECT decision_id,decision_revision FROM decisions "
                    "WHERE project_id=? AND status='effective'",
                    (project_id,),
                ).fetchone()
                current_revision = 0 if current is None else int(current["decision_revision"])
                if current_revision != expected_decision_revision:
                    raise Conflict(
                        f"决策版本已变化（当前 {current_revision}），并发批准只有一个有效修订"
                    )
                if current is not None:
                    cursor = self.connection.execute(
                        "UPDATE decisions SET status='superseded' WHERE decision_id=? AND status='effective'",
                        (current["decision_id"],),
                    )
                    if cursor.rowcount != 1:
                        raise Conflict("并发批准冲突：同一项目只能有一个有效修订")
                cursor = self.connection.execute(
                    "INSERT INTO decisions(project_id,decision_revision,scenario_id,evaluation_id,"
                    "snapshot_sha256,scenario_sha256,decision,rationale,supersedes_decision_id,status,"
                    "decided_by,decided_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        project_id, expected_decision_revision + 1, scenario_id, evaluation_id,
                        scenario["snapshot_sha256"], scenario["content_sha256"], decision, rationale,
                        None if current is None else current["decision_id"], "effective",
                        actor_id, now,
                    ),
                )
                decision_id = int(cursor.lastrowid)
                for dissent in parsed_dissents:
                    self.connection.execute(
                        "INSERT INTO dissents(decision_id,author_id,opinion,created_at) VALUES(?,?,?,?)",
                        (decision_id, dissent["author_id"], dissent["opinion"], now),
                    )
                self._audit(
                    "decision", str(decision_id), "decision.recorded", actor_id,
                    {
                        "project_id": project_id,
                        "decision_revision": expected_decision_revision + 1,
                        "scenario_id": scenario_id,
                        "evaluation_id": evaluation_id,
                        "decision": decision,
                        "dissent_count": len(parsed_dissents),
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("并发批准冲突：同一项目只能有一个有效修订") from exc
        return {
            "decision_id": decision_id,
            "project_id": project_id,
            "decision_revision": expected_decision_revision + 1,
            "decision": decision,
            "status": "effective",
            "snapshot_sha256": scenario["snapshot_sha256"],
            "scenario_sha256": scenario["content_sha256"],
            "evaluation_id": evaluation_id,
            "dissents_recorded": len(parsed_dissents),
        }

    def record_dissent(self, actor_id: str, decision_id: int, opinion: str) -> dict[str, Any]:
        """向既有投决追加反对意见；投决内容本身保持不变。"""

        self._require(actor_id, "dissent.record")
        opinion = required_text(opinion, "opinion", 512)
        decision = self.connection.execute(
            "SELECT decision_id FROM decisions WHERE decision_id=?", (decision_id,)
        ).fetchone()
        if decision is None:
            raise NotFound(f"投决不存在: {decision_id}")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO dissents(decision_id,author_id,opinion,created_at) VALUES(?,?,?,?)",
                (decision_id, actor_id, opinion, self._now()),
            )
            dissent_id = int(cursor.lastrowid)
            self._audit(
                "decision", str(decision_id), "dissent.recorded", actor_id,
                {"dissent_id": dissent_id},
            )
        return {"dissent_id": dissent_id, "decision_id": decision_id}

    def record_actual(
        self, actor_id: str, project_id: str, horizon_year: int, metrics_raw: object
    ) -> dict[str, Any]:
        """登记实绩并自动生成偏差；旧投决内容不被改写。"""

        self._require(actor_id, "actual.record")
        self._project(project_id)
        if isinstance(horizon_year, bool) or not isinstance(horizon_year, int) or horizon_year <= 0:
            raise ValidationFailed("horizon_year 必须是正整数")
        metrics = ActualMetrics.from_dict(metrics_raw if isinstance(metrics_raw, Mapping) else {})
        canonical = canonical_json(metrics.as_canonical())
        digest = hashlib.sha256(
            canonical_json({"project_id": project_id, "horizon_year": horizon_year, "metrics": metrics.as_canonical()})
            .encode("utf-8")
        ).hexdigest()
        effective = self.connection.execute(
            "SELECT * FROM decisions WHERE project_id=? AND status='effective'", (project_id,)
        ).fetchone()
        evaluation = None
        if effective is not None:
            evaluation = self.connection.execute(
                "SELECT * FROM evaluations WHERE evaluation_id=?", (effective["evaluation_id"],)
            ).fetchone()
        if evaluation is not None:
            result = json.loads(evaluation["result_json"])
            if horizon_year > len(result["annual"]):
                raise ValidationFailed("实绩年份超出已批准方案评价期")
            variance = deviation_variance(result, horizon_year, metrics.as_canonical())
        else:
            variance = None
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO actuals(project_id,horizon_year,metrics_json,content_sha256,recorded_by,recorded_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (project_id, horizon_year, canonical, digest, actor_id, self._now()),
                )
                actual_id = int(cursor.lastrowid)
                self._audit(
                    "actual", str(actual_id), "actual.recorded", actor_id,
                    {"project_id": project_id, "horizon_year": horizon_year, "content_sha256": digest},
                )
                deviation_id = None
                if variance is not None:
                    cursor = self.connection.execute(
                        "INSERT INTO deviations(project_id,actual_id,decision_id,evaluation_id,variance_json,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                        (
                            project_id, actual_id, effective["decision_id"], effective["evaluation_id"],
                            canonical_json(variance), actor_id, self._now(),
                        ),
                    )
                    deviation_id = int(cursor.lastrowid)
                    self._audit(
                        "deviation", str(deviation_id), "deviation.generated", actor_id,
                        {
                            "actual_id": actual_id,
                            "decision_id": effective["decision_id"],
                            "beyond_sensitivity_boundary": variance["beyond_sensitivity_boundary"],
                        },
                    )
        except sqlite3.IntegrityError as exc:
            raise Conflict("该评价年度的实绩已存在或内容摘要冲突") from exc
        return {
            "actual_id": actual_id,
            "project_id": project_id,
            "horizon_year": horizon_year,
            "content_sha256": digest,
            "deviation": None if deviation_id is None else {
                "deviation_id": deviation_id,
                "decision_id": effective["decision_id"],
                "decision_revision": effective["decision_revision"],
                "variance": variance,
            },
        }

    def derive_scenario(
        self,
        actor_id: str,
        scenario_id: str,
        actual_id: int,
        definition_raw: object,
    ) -> dict[str, Any]:
        """从实绩派生新情景；沿用原快照，旧投决保持不变。"""

        self._require(actor_id, "scenario.write")
        actual = self.connection.execute(
            "SELECT * FROM actuals WHERE actual_id=?", (actual_id,)
        ).fetchone()
        if actual is None:
            raise NotFound(f"实绩不存在: {actual_id}")
        deviation = self.connection.execute(
            "SELECT * FROM deviations WHERE actual_id=?", (actual_id,)
        ).fetchone()
        if deviation is None:
            raise InvalidState("实绩尚未关联有效投决，无法派生新情景")
        origin = self.connection.execute(
            "SELECT s.* FROM scenarios s JOIN decisions d ON d.scenario_id=s.scenario_id "
            "WHERE d.decision_id=?",
            (deviation["decision_id"],),
        ).fetchone()
        snapshot_row = self.connection.execute(
            "SELECT * FROM input_snapshots WHERE snapshot_id=?", (origin["snapshot_id"],)
        ).fetchone()
        definition = ScenarioDefinition.from_dict(
            definition_raw if isinstance(definition_raw, Mapping) else {}
        )
        inputs = self._snapshot_inputs(snapshot_row)
        self._check_definition_against_inputs(definition, inputs)
        result = self._insert_scenario(
            actor_id, scenario_id, actual["project_id"], snapshot_row, definition, actual_id
        )
        return result | {"derived_from_actual_id": actual_id, "origin_scenario_id": origin["scenario_id"]}

    def trace_decision(self, actor_id: str, decision_id: int) -> dict[str, Any]:
        """从结论追溯全部证据、假设与敏感性边界。"""

        self._require(actor_id, "trace.read")
        decision = self.connection.execute(
            "SELECT * FROM decisions WHERE decision_id=?", (decision_id,)
        ).fetchone()
        if decision is None:
            raise NotFound(f"投决不存在: {decision_id}")
        scenario = self.get_scenario(decision["scenario_id"])
        evaluation = self.connection.execute(
            "SELECT * FROM evaluations WHERE evaluation_id=?", (decision["evaluation_id"],)
        ).fetchone()
        snapshot = self.get_snapshot(scenario["snapshot_id"])
        contributions = []
        for category in CATEGORIES:
            reference = snapshot["composition"][category]
            row = self.connection.execute(
                "SELECT * FROM input_contributions WHERE contribution_id=?",
                (reference["contribution_id"],),
            ).fetchone()
            contributions.append({
                "category": category,
                "category_label": CATEGORY_LABELS[category],
                "contribution_id": row["contribution_id"],
                "version": row["version"],
                "content_sha256": row["content_sha256"],
                "submitted_by": row["submitted_by"],
                "submitted_at": row["submitted_at"],
                "payload": json.loads(row["payload_json"]),
            })
        dissents = self.connection.execute(
            "SELECT dissent_id,author_id,opinion,created_at FROM dissents WHERE decision_id=? "
            "ORDER BY dissent_id",
            (decision_id,),
        ).fetchall()
        deviations = self.connection.execute(
            "SELECT deviation_id,actual_id,variance_json,created_at FROM deviations WHERE decision_id=? "
            "ORDER BY deviation_id",
            (decision_id,),
        ).fetchall()
        lineage = self.connection.execute(
            "SELECT decision_id,decision_revision,decision,status,decided_by,decided_at "
            "FROM decisions WHERE project_id=? ORDER BY decision_revision",
            (decision["project_id"],),
        ).fetchall()
        entity_refs = (
            ("decision", str(decision_id)),
            ("scenario", decision["scenario_id"]),
            ("snapshot", scenario["snapshot_id"]),
            ("project", decision["project_id"]),
        )
        events: list[dict[str, Any]] = []
        for entity_type, entity_id in entity_refs:
            rows = self.connection.execute(
                "SELECT event_type,actor_id,payload_json,created_at FROM audit_events "
                "WHERE entity_type=? AND entity_id=? ORDER BY event_id",
                (entity_type, entity_id),
            ).fetchall()
            events.extend(
                {
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "event_type": row["event_type"],
                    "actor_id": row["actor_id"],
                    "payload": json.loads(row["payload_json"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            )
        return {
            "decision": dict(decision),
            "dissents": [dict(row) for row in dissents],
            "scenario": scenario,
            "evaluation": {
                "evaluation_id": evaluation["evaluation_id"],
                "algorithm_version": evaluation["algorithm_version"],
                "input_sha256": evaluation["input_sha256"],
                "snapshot_sha256": evaluation["snapshot_sha256"],
                "result": json.loads(evaluation["result_json"]),
            },
            "snapshot": snapshot,
            "contributions": contributions,
            "deviations": [
                dict(row) | {"variance": json.loads(row["variance_json"])} for row in deviations
            ],
            "decision_lineage": [dict(row) for row in lineage],
            "events": events,
        }

    def list_decisions(self, actor_id: str, project_id: str) -> dict[str, Any]:
        self._require(actor_id, "trace.read")
        self._project(project_id)
        rows = self.connection.execute(
            "SELECT decision_id,decision_revision,scenario_id,evaluation_id,decision,status,"
            "decided_by,decided_at FROM decisions WHERE project_id=? ORDER BY decision_revision",
            (project_id,),
        ).fetchall()
        return {"project_id": project_id, "decisions": [dict(row) for row in rows]}

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        """校验审计哈希链的完整性。"""

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
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
