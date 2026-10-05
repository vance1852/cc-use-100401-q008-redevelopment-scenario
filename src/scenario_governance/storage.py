"""二次开发情景治理的 SQLite 模式与事务辅助。

治理约束直接落在数据库层：
- 输入贡献、输入快照、方案、评价、实绩、偏差、反对意见与审计事件禁止 UPDATE/DELETE；
- 投决记录内容列不可改写，只允许 effective → superseded 的状态流转；
- 部分唯一索引保证同一项目并发批准时只有一个有效修订。
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path


SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN (
        'reservoir_engineer', 'facility_engineer', 'economist',
        'operations', 'decision_maker', 'auditor'
    )),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS redevelopment_projects (
    project_id TEXT PRIMARY KEY,
    field_name TEXT NOT NULL,
    source_pattern TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'closed')),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS input_contributions (
    contribution_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES redevelopment_projects(project_id),
    category TEXT NOT NULL CHECK (category IN (
        'reserves_version', 'well_group_response', 'water_cut_forecast',
        'facility_bottleneck', 'shutdown_window', 'capex', 'oil_price_assumption'
    )),
    version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE CHECK (length(content_sha256) = 64),
    submitted_by TEXT NOT NULL REFERENCES users(user_id),
    submitted_at TEXT NOT NULL,
    UNIQUE (project_id, category, version)
);

CREATE TABLE IF NOT EXISTS input_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES redevelopment_projects(project_id),
    composition_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE CHECK (length(content_sha256) = 64),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scenarios (
    scenario_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES redevelopment_projects(project_id),
    snapshot_id TEXT NOT NULL REFERENCES input_snapshots(snapshot_id),
    snapshot_sha256 TEXT NOT NULL CHECK (length(snapshot_sha256) = 64),
    name TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE CHECK (length(content_sha256) = 64),
    derived_from_actual_id INTEGER,
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluations (
    evaluation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id TEXT NOT NULL REFERENCES scenarios(scenario_id),
    snapshot_sha256 TEXT NOT NULL CHECK (length(snapshot_sha256) = 64),
    algorithm_version TEXT NOT NULL,
    input_sha256 TEXT NOT NULL CHECK (length(input_sha256) = 64),
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE (scenario_id, input_sha256)
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES redevelopment_projects(project_id),
    decision_revision INTEGER NOT NULL CHECK (decision_revision > 0),
    scenario_id TEXT NOT NULL REFERENCES scenarios(scenario_id),
    evaluation_id INTEGER NOT NULL REFERENCES evaluations(evaluation_id),
    snapshot_sha256 TEXT NOT NULL CHECK (length(snapshot_sha256) = 64),
    scenario_sha256 TEXT NOT NULL CHECK (length(scenario_sha256) = 64),
    decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    rationale TEXT NOT NULL,
    supersedes_decision_id INTEGER REFERENCES decisions(decision_id),
    status TEXT NOT NULL DEFAULT 'effective' CHECK (status IN ('effective', 'superseded')),
    decided_by TEXT NOT NULL REFERENCES users(user_id),
    decided_at TEXT NOT NULL,
    UNIQUE (project_id, decision_revision)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_effective_decision_per_project
ON decisions(project_id)
WHERE status = 'effective';

CREATE TABLE IF NOT EXISTS dissents (
    dissent_id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL REFERENCES decisions(decision_id),
    author_id TEXT NOT NULL REFERENCES users(user_id),
    opinion TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dissents_decision
ON dissents(decision_id, dissent_id);

CREATE TABLE IF NOT EXISTS actuals (
    actual_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES redevelopment_projects(project_id),
    horizon_year INTEGER NOT NULL CHECK (horizon_year > 0),
    metrics_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE CHECK (length(content_sha256) = 64),
    recorded_by TEXT NOT NULL REFERENCES users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE (project_id, horizon_year)
);

CREATE TABLE IF NOT EXISTS deviations (
    deviation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES redevelopment_projects(project_id),
    actual_id INTEGER NOT NULL UNIQUE REFERENCES actuals(actual_id),
    decision_id INTEGER NOT NULL REFERENCES decisions(decision_id),
    evaluation_id INTEGER NOT NULL REFERENCES evaluations(evaluation_id),
    variance_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_entity
ON audit_events(entity_type, entity_id, event_id);

CREATE TRIGGER IF NOT EXISTS contributions_no_update BEFORE UPDATE ON input_contributions
BEGIN SELECT RAISE(ABORT, '输入贡献不可改写'); END;

CREATE TRIGGER IF NOT EXISTS contributions_no_delete BEFORE DELETE ON input_contributions
BEGIN SELECT RAISE(ABORT, '输入贡献不可删除'); END;

CREATE TRIGGER IF NOT EXISTS snapshots_no_update BEFORE UPDATE ON input_snapshots
BEGIN SELECT RAISE(ABORT, '输入快照不可改写'); END;

CREATE TRIGGER IF NOT EXISTS snapshots_no_delete BEFORE DELETE ON input_snapshots
BEGIN SELECT RAISE(ABORT, '输入快照不可删除'); END;

CREATE TRIGGER IF NOT EXISTS scenarios_no_update BEFORE UPDATE ON scenarios
BEGIN SELECT RAISE(ABORT, '方案定义不可改写，请创建新方案'); END;

CREATE TRIGGER IF NOT EXISTS scenarios_no_delete BEFORE DELETE ON scenarios
BEGIN SELECT RAISE(ABORT, '方案定义不可删除'); END;

CREATE TRIGGER IF NOT EXISTS evaluations_no_update BEFORE UPDATE ON evaluations
BEGIN SELECT RAISE(ABORT, '评价结果不可改写'); END;

CREATE TRIGGER IF NOT EXISTS evaluations_no_delete BEFORE DELETE ON evaluations
BEGIN SELECT RAISE(ABORT, '评价结果不可删除'); END;

CREATE TRIGGER IF NOT EXISTS decisions_content_immutable BEFORE UPDATE ON decisions
WHEN OLD.project_id IS NOT NEW.project_id
  OR OLD.decision_revision IS NOT NEW.decision_revision
  OR OLD.scenario_id IS NOT NEW.scenario_id
  OR OLD.evaluation_id IS NOT NEW.evaluation_id
  OR OLD.snapshot_sha256 IS NOT NEW.snapshot_sha256
  OR OLD.scenario_sha256 IS NOT NEW.scenario_sha256
  OR OLD.decision IS NOT NEW.decision
  OR OLD.rationale IS NOT NEW.rationale
  OR OLD.supersedes_decision_id IS NOT NEW.supersedes_decision_id
  OR OLD.decided_by IS NOT NEW.decided_by
  OR OLD.decided_at IS NOT NEW.decided_at
BEGIN SELECT RAISE(ABORT, '投决内容不可改写，只能以新修订取代'); END;

CREATE TRIGGER IF NOT EXISTS decisions_no_delete BEFORE DELETE ON decisions
BEGIN SELECT RAISE(ABORT, '投决记录不可删除'); END;

CREATE TRIGGER IF NOT EXISTS dissents_no_update BEFORE UPDATE ON dissents
BEGIN SELECT RAISE(ABORT, '反对意见不可改写'); END;

CREATE TRIGGER IF NOT EXISTS dissents_no_delete BEFORE DELETE ON dissents
BEGIN SELECT RAISE(ABORT, '反对意见不可删除'); END;

CREATE TRIGGER IF NOT EXISTS actuals_no_update BEFORE UPDATE ON actuals
BEGIN SELECT RAISE(ABORT, '实绩记录不可改写'); END;

CREATE TRIGGER IF NOT EXISTS actuals_no_delete BEFORE DELETE ON actuals
BEGIN SELECT RAISE(ABORT, '实绩记录不可删除'); END;

CREATE TRIGGER IF NOT EXISTS deviations_no_update BEFORE UPDATE ON deviations
BEGIN SELECT RAISE(ABORT, '偏差记录不可改写'); END;

CREATE TRIGGER IF NOT EXISTS deviations_no_delete BEFORE DELETE ON deviations
BEGIN SELECT RAISE(ABORT, '偏差记录不可删除'); END;

CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, '审计事件不可改写'); END;

CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, '审计事件不可删除'); END;
"""

REQUIRED_TABLES = frozenset({
    "schema_meta", "users", "redevelopment_projects", "input_contributions",
    "input_snapshots", "scenarios", "evaluations", "decisions", "dissents",
    "actuals", "deviations", "audit_events",
})


def connect(path: str | Path) -> sqlite3.Connection:
    """打开连接并启用严格的事务与外键设置。

    check_same_thread=False 允许 HTTP 服务的工作线程共享同一连接；
    并发请求由 JsonApplication 的调度锁串行化，事务语义不受影响。
    """

    connection = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    """显式事务；异常时保证回滚。"""

    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def initialize(connection: sqlite3.Connection) -> None:
    """初始化基础资料表，重复执行不改变已有数据。"""

    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


def inspect_schema(connection: sqlite3.Connection) -> dict[str, object]:
    """返回适合机器检查的数据库结构摘要。"""

    table_rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = tuple(row["name"] for row in table_rows)
    version_row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    missing = sorted(REQUIRED_TABLES - set(tables))
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    return {
        "tables": tables,
        "missing_tables": missing,
        "schema_version": None if version_row is None else version_row["value"],
        "foreign_keys": bool(foreign_keys),
    }
