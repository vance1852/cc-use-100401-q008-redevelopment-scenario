"""二次开发情景治理的 SQLite 模式与事务辅助。"""

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

CREATE TABLE IF NOT EXISTS gov_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('reservoir','facility','finance','decision_maker','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS input_snapshots (
    snapshot_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    asset_id TEXT NOT NULL,
    title TEXT NOT NULL,
    canonical_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    created_by TEXT NOT NULL REFERENCES gov_users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, revision),
    UNIQUE (content_sha256)
);

CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL,
    name TEXT NOT NULL,
    snapshot_sha256 TEXT NOT NULL,
    parent_plan_id TEXT,
    canonical_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    state TEXT NOT NULL DEFAULT 'candidate'
        CHECK (state IN ('candidate','evaluated','approved','rejected','superseded','closed')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    evaluation_json TEXT,
    evaluation_version TEXT,
    evaluated_by TEXT REFERENCES gov_users(user_id),
    evaluated_at TEXT,
    created_by TEXT NOT NULL REFERENCES gov_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE (content_sha256),
    FOREIGN KEY (snapshot_sha256) REFERENCES input_snapshots(content_sha256),
    FOREIGN KEY (parent_plan_id) REFERENCES plans(plan_id)
);

-- 并发批准只能有一个有效修订：每个油田至多一行 active 投决。
CREATE TABLE IF NOT EXISTS investment_decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id TEXT NOT NULL,
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    plan_revision INTEGER NOT NULL,
    snapshot_sha256 TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('approved','rejected')),
    conclusion TEXT NOT NULL,
    decided_by TEXT NOT NULL REFERENCES gov_users(user_id),
    decided_at TEXT NOT NULL,
    superseded_by INTEGER REFERENCES investment_decisions(decision_id),
    superseded_at TEXT,
    UNIQUE (plan_id, plan_revision)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_decision_per_asset
ON investment_decisions(asset_id)
WHERE superseded_by IS NULL AND outcome = 'approved';

CREATE TABLE IF NOT EXISTS decision_objections (
    objection_id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL REFERENCES investment_decisions(decision_id),
    raised_by TEXT NOT NULL REFERENCES gov_users(user_id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS actual_performances (
    actual_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    period_year INTEGER NOT NULL CHECK (period_year BETWEEN 1 AND 30),
    canonical_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    recorded_by TEXT NOT NULL REFERENCES gov_users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE (plan_id, period_year)
);

CREATE TABLE IF NOT EXISTS variance_reports (
    variance_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    period_year INTEGER NOT NULL,
    actual_id INTEGER NOT NULL REFERENCES actual_performances(actual_id),
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES gov_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE (plan_id, period_year)
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
"""

REQUIRED_TABLES = frozenset({
    "schema_meta", "gov_users", "input_snapshots", "plans", "investment_decisions",
    "decision_objections", "actual_performances", "variance_reports", "audit_events",
})


def connect(path: str | Path) -> sqlite3.Connection:
    """打开连接并启用严格的事务与外键设置。"""

    # ThreadingHTTPServer 在工作线程处理请求，所有写事务都以 BEGIN IMMEDIATE
    # 立即获取库级写锁，跨线程共享连接在显式事务模型下安全。
    connection = sqlite3.connect(
        str(path), isolation_level=None, check_same_thread=False)
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
    """初始化表结构，重复执行不改变已有数据。"""

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
