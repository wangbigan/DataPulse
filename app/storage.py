from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any

import duckdb
import yaml

from .metrics import METRICS
from .settings import Settings


def utc_now_sql() -> str:
    return "now()"


class MetadataStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.settings.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.report_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = duckdb.connect(str(self.settings.metadata_path))
        self.init_schema()
        self.seed_metric_registry()
        self.import_sensitive_config()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] | None = None) -> None:
        with self._lock:
            self._conn.execute(sql, params or [])

    def query(self, sql: str, params: tuple[Any, ...] | list[Any] | None = None) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(sql, params or [])
            columns = [desc[0] for desc in cur.description or []]
            return [dict(zip(columns, row)) for row in cur.fetchall()]

    def scalar(self, sql: str, params: tuple[Any, ...] | list[Any] | None = None) -> Any:
        rows = self.query(sql, params)
        if not rows:
            return None
        return next(iter(rows[0].values()))

    def init_schema(self) -> None:
        ddl = [
            """
            CREATE TABLE IF NOT EXISTS data_source (
                source_id VARCHAR PRIMARY KEY,
                name VARCHAR NOT NULL,
                dialect VARCHAR NOT NULL,
                conn_uri VARCHAR NOT NULL,
                options_json VARCHAR NOT NULL DEFAULT '{}',
                created_at TIMESTAMP NOT NULL DEFAULT now(),
                updated_at TIMESTAMP NOT NULL DEFAULT now()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS scan_snapshot (
                snapshot_id VARCHAR PRIMARY KEY,
                source_id VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                started_at TIMESTAMP,
                finished_at TIMESTAMP,
                scope_json VARCHAR NOT NULL DEFAULT '{}',
                metric_def_version VARCHAR NOT NULL,
                error_message VARCHAR
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS scan_task (
                task_id VARCHAR PRIMARY KEY,
                snapshot_id VARCHAR NOT NULL,
                source_id VARCHAR NOT NULL,
                task_type VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                priority INTEGER NOT NULL,
                weight DOUBLE NOT NULL DEFAULT 1,
                attempt INTEGER NOT NULL DEFAULT 0,
                crash_count INTEGER NOT NULL DEFAULT 0,
                error_message VARCHAR,
                started_at TIMESTAMP,
                finished_at TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS meta_table (
                snapshot_id VARCHAR NOT NULL,
                table_id VARCHAR NOT NULL,
                schema_name VARCHAR NOT NULL,
                table_name VARCHAR NOT NULL,
                table_comment VARCHAR,
                primary_key VARCHAR,
                column_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (snapshot_id, table_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS meta_column (
                snapshot_id VARCHAR NOT NULL,
                column_id VARCHAR NOT NULL,
                table_id VARCHAR NOT NULL,
                schema_name VARCHAR NOT NULL,
                table_name VARCHAR NOT NULL,
                column_name VARCHAR NOT NULL,
                data_type VARCHAR NOT NULL,
                is_nullable BOOLEAN NOT NULL DEFAULT true,
                ordinal_position INTEGER NOT NULL,
                column_comment VARCHAR,
                is_sensitive BOOLEAN NOT NULL DEFAULT false,
                sensitive_action VARCHAR,
                sensitive_reason VARCHAR,
                PRIMARY KEY (snapshot_id, column_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS table_stat (
                snapshot_id VARCHAR NOT NULL,
                table_id VARCHAR NOT NULL,
                row_count BIGINT,
                avg_fill_rate DOUBLE,
                avg_valid_rate DOUBLE,
                pk_duplicate_rows BIGINT,
                pk_duplicate_rate DOUBLE,
                pk_duplicate_skipped_reason VARCHAR,
                date_column VARCHAR,
                min_date VARCHAR,
                max_date VARCHAR,
                computed_at TIMESTAMP NOT NULL DEFAULT now(),
                PRIMARY KEY (snapshot_id, table_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS column_stat (
                snapshot_id VARCHAR NOT NULL,
                column_id VARCHAR NOT NULL,
                table_id VARCHAR NOT NULL,
                row_count BIGINT,
                null_count BIGINT,
                empty_count BIGINT,
                placeholder_count BIGINT,
                non_empty_count BIGINT,
                distinct_count BIGINT,
                fill_rate DOUBLE,
                valid_rate DOUBLE,
                duplicate_rate DOUBLE,
                min_value VARCHAR,
                max_value VARCHAR,
                avg_value DOUBLE,
                p50_value DOUBLE,
                is_estimated BOOLEAN NOT NULL DEFAULT false,
                skipped_reason VARCHAR,
                computed_at TIMESTAMP NOT NULL DEFAULT now(),
                PRIMARY KEY (snapshot_id, column_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS value_dist (
                snapshot_id VARCHAR NOT NULL,
                column_id VARCHAR NOT NULL,
                value_label VARCHAR NOT NULL,
                value_count BIGINT NOT NULL,
                ratio DOUBLE,
                is_masked BOOLEAN NOT NULL DEFAULT false,
                computed_at TIMESTAMP NOT NULL DEFAULT now()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS sample_data (
                snapshot_id VARCHAR NOT NULL,
                column_id VARCHAR NOT NULL,
                sample_value VARCHAR,
                is_masked BOOLEAN NOT NULL DEFAULT false,
                computed_at TIMESTAMP NOT NULL DEFAULT now()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS meta_relation (
                snapshot_id VARCHAR NOT NULL,
                relation_id VARCHAR NOT NULL,
                relation_type VARCHAR NOT NULL,
                constraint_name VARCHAR,
                child_table_id VARCHAR NOT NULL,
                parent_table_id VARCHAR NOT NULL,
                child_schema VARCHAR NOT NULL,
                child_table VARCHAR NOT NULL,
                child_columns_json VARCHAR NOT NULL,
                parent_schema VARCHAR NOT NULL,
                parent_table VARCHAR NOT NULL,
                parent_columns_json VARCHAR NOT NULL,
                compare_rule VARCHAR NOT NULL DEFAULT 'raw',
                computed_at TIMESTAMP NOT NULL DEFAULT now(),
                PRIMARY KEY (snapshot_id, relation_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS relation_stat (
                snapshot_id VARCHAR NOT NULL,
                relation_id VARCHAR NOT NULL,
                child_table_id VARCHAR NOT NULL,
                parent_table_id VARCHAR NOT NULL,
                child_fk_non_empty_rows BIGINT,
                matched_rows BIGINT,
                orphan_rows BIGINT,
                match_rate DOUBLE,
                orphan_rate DOUBLE,
                orphan_samples_json VARCHAR,
                skipped_reason VARCHAR,
                computed_at TIMESTAMP NOT NULL DEFAULT now(),
                PRIMARY KEY (snapshot_id, relation_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS sensitive_config (
                pattern VARCHAR PRIMARY KEY,
                action VARCHAR NOT NULL,
                reason VARCHAR
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS dim_config (
                source_id VARCHAR,
                schema_name VARCHAR,
                table_name VARCHAR,
                column_name VARCHAR,
                dimension_name VARCHAR,
                PRIMARY KEY (source_id, schema_name, table_name, column_name)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS metric_registry (
                metric_code VARCHAR NOT NULL,
                version VARCHAR NOT NULL,
                name VARCHAR NOT NULL,
                definition VARCHAR NOT NULL,
                formula VARCHAR NOT NULL,
                denominator VARCHAR NOT NULL,
                boundary VARCHAR NOT NULL,
                PRIMARY KEY (metric_code, version)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                audit_id VARCHAR PRIMARY KEY,
                event_type VARCHAR NOT NULL,
                detail_json VARCHAR NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT now()
            )
            """,
        ]
        for statement in ddl:
            self.execute(statement)
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        self._add_missing_columns(
            "table_stat",
            {
                "pk_duplicate_rows": "BIGINT",
                "pk_duplicate_rate": "DOUBLE",
                "pk_duplicate_skipped_reason": "VARCHAR",
            },
        )

    def _add_missing_columns(self, table: str, columns: dict[str, str]) -> None:
        existing = {
            row["column_name"]
            for row in self.query(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'main' AND table_name = ?
                """,
                [table],
            )
        }
        for column, data_type in columns.items():
            if column not in existing:
                self.execute(f"ALTER TABLE {table} ADD COLUMN {column} {data_type}")

    def seed_metric_registry(self) -> None:
        version = self.settings.metric_def_version
        self.execute("DELETE FROM metric_registry WHERE version = ?", [version])
        for metric in METRICS:
            self.execute(
                """
                INSERT INTO metric_registry
                (metric_code, version, name, definition, formula, denominator, boundary)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    metric["metric_code"],
                    version,
                    metric["name"],
                    metric["definition"],
                    metric["formula"],
                    metric["denominator"],
                    metric["boundary"],
                ],
            )

    def import_sensitive_config(self) -> None:
        path = self.settings.sensitive_config_path
        if not path.exists():
            return
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        self.execute("DELETE FROM sensitive_config")
        for item in data.get("sensitive", []):
            self.execute(
                "INSERT INTO sensitive_config (pattern, action, reason) VALUES (?, ?, ?)",
                [item["pattern"], item.get("action", "skip"), item.get("reason")],
            )
        self.execute("DELETE FROM dim_config")
        for item in data.get("dimensions", []):
            self.execute(
                """
                INSERT INTO dim_config
                (source_id, schema_name, table_name, column_name, dimension_name)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    item.get("source_id"),
                    item.get("schema_name", "main"),
                    item["table_name"],
                    item["column_name"],
                    item.get("dimension_name", "机构"),
                ],
            )

    def add_audit(self, event_type: str, detail: dict[str, Any]) -> None:
        self.execute(
            "INSERT INTO audit_log (audit_id, event_type, detail_json) VALUES (?, ?, ?)",
            [str(uuid.uuid4()), event_type, json.dumps(detail, ensure_ascii=False)],
        )
        self.checkpoint()

    def checkpoint(self) -> None:
        with self._lock:
            self._conn.execute("CHECKPOINT")

    def replace_rows(self, table: str, where_sql: str, where_params: list[Any], rows: list[dict[str, Any]]) -> None:
        if not rows:
            self.execute(f"DELETE FROM {table} WHERE {where_sql}", where_params)
            return
        columns = list(rows[0].keys())
        placeholders = ", ".join(["?"] * len(columns))
        column_sql = ", ".join(columns)
        with self._lock:
            self._conn.execute(f"DELETE FROM {table} WHERE {where_sql}", where_params)
            for row in rows:
                self._conn.execute(
                    f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})",
                    [row[column] for column in columns],
                )


def new_id() -> str:
    return str(uuid.uuid4())


def read_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def write_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
