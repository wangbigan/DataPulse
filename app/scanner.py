from __future__ import annotations

import json
import re
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from .datasource import (
    ColumnRef,
    ForeignKeyRef,
    TableRef,
    escape_literal,
    is_date_type,
    is_numeric_type,
    is_string_type,
    mask_value,
    open_source,
    sentinel_label,
)
from .settings import Settings
from .storage import MetadataStore, new_id, write_json


@dataclass(frozen=True)
class SensitivePolicy:
    is_sensitive: bool
    action: str | None = None
    reason: str | None = None


class ScanManager:
    def __init__(self, store: MetadataStore, settings: Settings):
        self.store = store
        self.settings = settings
        self.executor = ThreadPoolExecutor(max_workers=1)
        self._control_lock = threading.RLock()
        self._paused: set[str] = set()
        self._recover_stale_running()

    def _recover_stale_running(self) -> None:
        self.store.execute(
            """
            UPDATE scan_task
            SET status = 'ready', crash_count = crash_count + 1, started_at = NULL
            WHERE status = 'running'
            """
        )
        self.store.execute(
            """
            UPDATE scan_task
            SET status = 'failed', error_message = 'crash_count >= 3'
            WHERE status = 'ready' AND crash_count >= 3
            """
        )
        self.store.execute(
            "UPDATE scan_snapshot SET status = 'partial', finished_at = now() WHERE status = 'running'"
        )

    def create_snapshot(self, source_id: str, tables: list[str] | None = None) -> str:
        running = self.store.query(
            "SELECT snapshot_id FROM scan_snapshot WHERE source_id = ? AND status IN ('created', 'running', 'paused')",
            [source_id],
        )
        if running:
            raise ValueError("同一数据源已有扫描在运行或暂停，MVP 保持同源单活快照。")
        source = self.get_source(source_id)
        snapshot_id = new_id()
        scope = {"tables": tables or []}
        self.store.execute(
            """
            INSERT INTO scan_snapshot
            (snapshot_id, source_id, status, scope_json, metric_def_version)
            VALUES (?, ?, 'created', ?, ?)
            """,
            [snapshot_id, source_id, write_json(scope), self.settings.metric_def_version],
        )
        self._insert_task(snapshot_id, source_id, "struct", 0, 1)
        self.executor.submit(self._run_snapshot, snapshot_id, source, tables or [])
        return snapshot_id

    def get_source(self, source_id: str) -> dict[str, Any]:
        rows = self.store.query("SELECT * FROM data_source WHERE source_id = ?", [source_id])
        if not rows:
            raise ValueError("数据源不存在。")
        return rows[0]

    def delete_snapshot(self, snapshot_id: str) -> None:
        with self._control_lock:
            self._paused.discard(snapshot_id)
        self._delete_snapshot_rows(snapshot_id)
        self.store.add_audit("snapshot_deleted", {"snapshot_id": snapshot_id})

    def _delete_snapshot_rows(self, snapshot_id: str) -> None:
        for table in [
            "value_dist",
            "sample_data",
            "relation_stat",
            "meta_relation",
            "column_stat",
            "table_stat",
            "meta_column",
            "meta_table",
            "scan_task",
            "scan_snapshot",
        ]:
            self.store.execute(f"DELETE FROM {table} WHERE snapshot_id = ?", [snapshot_id])
        self.store.checkpoint()

    def pause(self, snapshot_id: str) -> None:
        with self._control_lock:
            self._paused.add(snapshot_id)
        self.store.execute(
            "UPDATE scan_snapshot SET status = 'paused' WHERE snapshot_id = ? AND status = 'running'",
            [snapshot_id],
        )

    def resume(self, snapshot_id: str) -> None:
        source_rows = self.store.query(
            """
            SELECT ds.*
            FROM scan_snapshot ss
            JOIN data_source ds ON ds.source_id = ss.source_id
            WHERE ss.snapshot_id = ?
            """,
            [snapshot_id],
        )
        if not source_rows:
            raise ValueError("快照不存在。")
        with self._control_lock:
            self._paused.discard(snapshot_id)
        self.store.execute(
            "UPDATE scan_snapshot SET status = 'running' WHERE snapshot_id = ? AND status = 'paused'",
            [snapshot_id],
        )
        scope = json.loads(
            self.store.scalar("SELECT scope_json FROM scan_snapshot WHERE snapshot_id = ?", [snapshot_id]) or "{}"
        )
        self.executor.submit(self._run_snapshot, snapshot_id, source_rows[0], scope.get("tables", []))

    def progress(self, snapshot_id: str) -> dict[str, Any]:
        rows = self.store.query(
            """
            SELECT
                SUM(weight) AS total_weight,
                SUM(CASE WHEN status IN ('done', 'skipped') THEN weight ELSE 0 END) AS done_weight,
                SUM(CASE WHEN status = 'failed' THEN weight ELSE 0 END) AS failed_weight,
                COUNT(*) AS total_tasks,
                SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done_tasks,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_tasks
            FROM scan_task
            WHERE snapshot_id = ?
            """,
            [snapshot_id],
        )
        snapshot = self.store.query("SELECT * FROM scan_snapshot WHERE snapshot_id = ?", [snapshot_id])
        stat = rows[0] if rows else {}
        total_weight = float(stat.get("total_weight") or 0)
        done_weight = float(stat.get("done_weight") or 0)
        return {
            "snapshot": snapshot[0] if snapshot else None,
            "progress": round(done_weight / total_weight, 4) if total_weight else 0,
            "total_tasks": int(stat.get("total_tasks") or 0),
            "done_tasks": int(stat.get("done_tasks") or 0),
            "failed_tasks": int(stat.get("failed_tasks") or 0),
            "failed_weight": float(stat.get("failed_weight") or 0),
        }

    def _insert_task(self, snapshot_id: str, source_id: str, task_type: str, priority: int, weight: float) -> str:
        task_id = new_id()
        self.store.execute(
            """
            INSERT INTO scan_task
            (task_id, snapshot_id, source_id, task_type, status, priority, weight)
            VALUES (?, ?, ?, ?, 'ready', ?, ?)
            """,
            [task_id, snapshot_id, source_id, task_type, priority, weight],
        )
        return task_id

    def _run_snapshot(self, snapshot_id: str, source: dict[str, Any], selected_tables: list[str]) -> None:
        conn = None
        try:
            if not self._snapshot_exists(snapshot_id):
                return
            self.store.execute(
                "UPDATE scan_snapshot SET status = 'running', started_at = COALESCE(started_at, now()) WHERE snapshot_id = ?",
                [snapshot_id],
            )
            conn = open_source(source["dialect"], source["conn_uri"])
            struct_task = self._next_task(snapshot_id, "struct")
            if struct_task and struct_task["status"] != "done":
                self._run_task(struct_task["task_id"], lambda: self._scan_struct(conn, snapshot_id, source["source_id"], selected_tables))
            if self._should_wait(snapshot_id):
                return

            tables = self.store.query(
                "SELECT * FROM meta_table WHERE snapshot_id = ? ORDER BY schema_name, table_name",
                [snapshot_id],
            )
            for table_row in tables:
                if self._should_wait(snapshot_id):
                    return
                table = TableRef(
                    table_row["schema_name"],
                    table_row["table_name"],
                    table_row.get("table_comment"),
                    table_row.get("primary_key"),
                )
                row_task = self._ensure_table_task(snapshot_id, source["source_id"], "rowcount", table_row["table_id"], 1, 2)
                if row_task["status"] != "done":
                    self._run_task(row_task["task_id"], lambda table=table, table_row=table_row: self._scan_rowcount(conn, snapshot_id, table_row["table_id"], table))
                if self._should_wait(snapshot_id):
                    return

                col_task = self._ensure_table_task(snapshot_id, source["source_id"], "column", table_row["table_id"], 2, 10)
                if col_task["status"] != "done":
                    self._run_task(col_task["task_id"], lambda table=table, table_row=table_row: self._scan_columns(conn, snapshot_id, table_row["table_id"], table))
                if self._should_wait(snapshot_id):
                    return

                dist_task = self._ensure_table_task(snapshot_id, source["source_id"], "value_dist_sample", table_row["table_id"], 3, 6)
                if dist_task["status"] != "done":
                    self._run_task(dist_task["task_id"], lambda table=table, table_row=table_row: self._scan_value_dist_and_samples(conn, snapshot_id, table_row["table_id"], table))
                if self._should_wait(snapshot_id):
                    return

            relation_task = self._ensure_snapshot_task(snapshot_id, source["source_id"], "relation", 8, 4)
            if relation_task["status"] != "done":
                self._run_task(relation_task["task_id"], lambda: self._scan_relations(conn, snapshot_id))
            if self._should_wait(snapshot_id):
                return

            finalize_task = self._ensure_snapshot_task(snapshot_id, source["source_id"], "finalize", 9, 1)
            if finalize_task["status"] != "done":
                self._run_task(finalize_task["task_id"], lambda: self._finalize(snapshot_id))
            self._finish_snapshot(snapshot_id)
        except Exception as exc:
            self.store.execute(
                "UPDATE scan_snapshot SET status = 'failed', finished_at = now(), error_message = ? WHERE snapshot_id = ?",
                [f"{exc}\n{traceback.format_exc(limit=5)}", snapshot_id],
            )
        finally:
            if conn:
                conn.close()
            if not self._snapshot_exists(snapshot_id):
                self._delete_snapshot_rows(snapshot_id)

    def _next_task(self, snapshot_id: str, task_type: str) -> dict[str, Any] | None:
        rows = self.store.query(
            "SELECT * FROM scan_task WHERE snapshot_id = ? AND task_type = ? ORDER BY priority LIMIT 1",
            [snapshot_id, task_type],
        )
        return rows[0] if rows else None

    def _ensure_snapshot_task(self, snapshot_id: str, source_id: str, task_type: str, priority: int, weight: float) -> dict[str, Any]:
        existing = self._next_task(snapshot_id, task_type)
        if existing:
            return existing
        task_id = self._insert_task(snapshot_id, source_id, task_type, priority, weight)
        return self.store.query("SELECT * FROM scan_task WHERE task_id = ?", [task_id])[0]

    def _ensure_table_task(
        self,
        snapshot_id: str,
        source_id: str,
        task_type: str,
        table_id: str,
        priority: int,
        weight: float,
    ) -> dict[str, Any]:
        typed = f"{task_type}:{table_id}"
        existing = self._next_task(snapshot_id, typed)
        if existing:
            return existing
        task_id = self._insert_task(snapshot_id, source_id, typed, priority, weight)
        return self.store.query("SELECT * FROM scan_task WHERE task_id = ?", [task_id])[0]

    def _run_task(self, task_id: str, action) -> None:
        self.store.execute(
            "UPDATE scan_task SET status = 'running', started_at = now(), attempt = attempt + 1 WHERE task_id = ?",
            [task_id],
        )
        try:
            action()
        except Exception as exc:
            self.store.execute(
                "UPDATE scan_task SET status = 'failed', finished_at = now(), error_message = ? WHERE task_id = ?",
                [f"{exc}\n{traceback.format_exc(limit=5)}", task_id],
            )
            return
        self.store.execute(
            "UPDATE scan_task SET status = 'done', finished_at = now(), error_message = NULL WHERE task_id = ?",
            [task_id],
        )

    def _scan_struct(
        self,
        conn,
        snapshot_id: str,
        source_id: str,
        selected_tables: list[str],
    ) -> None:
        selected = {name.lower() for name in selected_tables}
        tables = [
            table
            for table in conn.list_tables()
            if not selected or table.table_name.lower() in selected or f"{table.schema_name}.{table.table_name}".lower() in selected
        ]
        policies = self._sensitive_patterns()
        table_rows = []
        column_rows = []
        table_id_by_key: dict[tuple[str, str], str] = {}
        for table in tables:
            table_id = self._table_id(snapshot_id, table)
            table_id_by_key[(table.schema_name, table.table_name)] = table_id
            columns = conn.list_columns(table)
            table_rows.append(
                {
                    "snapshot_id": snapshot_id,
                    "table_id": table_id,
                    "schema_name": table.schema_name,
                    "table_name": table.table_name,
                    "table_comment": table.table_comment,
                    "primary_key": table.primary_key,
                    "column_count": len(columns),
                }
            )
            for column in columns:
                policy = self._sensitive_policy(column, policies)
                column_rows.append(
                    {
                        "snapshot_id": snapshot_id,
                        "column_id": self._column_id(snapshot_id, table, column.column_name),
                        "table_id": table_id,
                        "schema_name": column.schema_name,
                        "table_name": column.table_name,
                        "column_name": column.column_name,
                        "data_type": column.data_type,
                        "is_nullable": column.is_nullable,
                        "ordinal_position": column.ordinal_position,
                        "column_comment": column.column_comment,
                        "is_sensitive": policy.is_sensitive,
                        "sensitive_action": policy.action,
                        "sensitive_reason": policy.reason,
                    }
                )
        self.store.replace_rows("meta_table", "snapshot_id = ?", [snapshot_id], table_rows)
        self.store.replace_rows("meta_column", "snapshot_id = ?", [snapshot_id], column_rows)
        relation_rows = self._physical_relation_rows(conn, snapshot_id, table_id_by_key)
        self.store.replace_rows("meta_relation", "snapshot_id = ?", [snapshot_id], relation_rows)

    def _scan_rowcount(self, conn, snapshot_id: str, table_id: str, table: TableRef) -> None:
        row = conn.query(f"SELECT COUNT(*) AS row_count FROM {conn.table_sql(table)}")[0]
        row_count = int(row["row_count"] or 0)
        pk_duplicate_rows, pk_duplicate_rate, pk_duplicate_skipped_reason = self._pk_duplicate_stats(conn, table, row_count)
        self.store.replace_rows(
            "table_stat",
            "snapshot_id = ? AND table_id = ?",
            [snapshot_id, table_id],
            [
                {
                    "snapshot_id": snapshot_id,
                    "table_id": table_id,
                    "row_count": row_count,
                    "avg_fill_rate": None,
                    "avg_valid_rate": None,
                    "pk_duplicate_rows": pk_duplicate_rows,
                    "pk_duplicate_rate": pk_duplicate_rate,
                    "pk_duplicate_skipped_reason": pk_duplicate_skipped_reason,
                    "date_column": None,
                    "min_date": None,
                    "max_date": None,
                }
            ],
        )

    def _pk_duplicate_stats(self, conn, table: TableRef, row_count: int) -> tuple[int | None, float | None, str | None]:
        key_columns = self._key_columns(table.primary_key)
        if not key_columns:
            return None, None, "no_declared_primary_key"
        if row_count <= 0:
            return 0, None, None
        key_sql = ", ".join(conn.quote(column) for column in key_columns)
        try:
            row = conn.query(
                f"""
                SELECT COALESCE(SUM(cnt - 1), 0) AS duplicate_rows
                FROM (
                    SELECT {key_sql}, COUNT(*) AS cnt
                    FROM {conn.table_sql(table)}
                    GROUP BY {key_sql}
                    HAVING COUNT(*) > 1
                ) pk_dup
                """
            )[0]
        except Exception as exc:
            return None, None, f"pk_duplicate_failed: {exc}"
        duplicate_rows = int(row["duplicate_rows"] or 0)
        return duplicate_rows, duplicate_rows / row_count, None

    def _scan_columns(self, conn, snapshot_id: str, table_id: str, table: TableRef) -> None:
        columns = self.store.query(
            "SELECT * FROM meta_column WHERE snapshot_id = ? AND table_id = ? ORDER BY ordinal_position",
            [snapshot_id, table_id],
        )
        table_stat = self.store.query(
            "SELECT row_count FROM table_stat WHERE snapshot_id = ? AND table_id = ?",
            [snapshot_id, table_id],
        )
        row_count = int(table_stat[0]["row_count"] or 0) if table_stat else 0
        stat_rows = []
        for column in columns:
            col = ColumnRef(
                column["schema_name"],
                column["table_name"],
                column["column_name"],
                column["data_type"],
                bool(column["is_nullable"]),
                int(column["ordinal_position"]),
                column.get("column_comment"),
            )
            stat_rows.append(self._column_stats(conn, snapshot_id, table_id, table, col, column, row_count))
        self.store.replace_rows("column_stat", "snapshot_id = ? AND table_id = ?", [snapshot_id, table_id], stat_rows)
        self._update_table_averages(snapshot_id, table_id)

    def _column_stats(
        self,
        conn,
        snapshot_id: str,
        table_id: str,
        table: TableRef,
        col: ColumnRef,
        column_row: dict[str, Any],
        row_count: int,
    ) -> dict[str, Any]:
        qcol = conn.quote(col.column_name)
        table_sql = conn.table_sql(table)
        string_col = is_string_type(col.data_type)
        numeric_col = is_numeric_type(col.data_type)
        sensitive_skip = column_row.get("sensitive_action") == "skip"
        text_expr = conn.text_cast_sql(qcol)

        empty_sql = f"SUM(CASE WHEN {qcol} IS NOT NULL AND TRIM({text_expr}) = '' THEN 1 ELSE 0 END)"
        placeholder_sql = "0"
        if string_col:
            values = ", ".join(conn.literal_sql(value) for value in self.settings.placeholder_values)
            placeholder_sql = (
                f"SUM(CASE WHEN {qcol} IS NOT NULL AND TRIM({text_expr}) IN ({values}) "
                "THEN 1 ELSE 0 END)"
            )
        if not string_col:
            empty_sql = "0"

        distinct_sql = "NULL"
        distinct_skipped = row_count > self.settings.distinct_exact_row_limit
        if not distinct_skipped and not sensitive_skip:
            distinct_sql = f"COUNT(DISTINCT {qcol})"

        min_sql = "NULL"
        max_sql = "NULL"
        avg_sql = "NULL"
        if not sensitive_skip:
            min_sql = conn.text_cast_sql(f"MIN({qcol})")
            max_sql = conn.text_cast_sql(f"MAX({qcol})")
            if numeric_col:
                avg_sql = f"AVG(CAST({qcol} AS DOUBLE))"

        stats_sql = f"""
            SELECT
                COUNT(*) AS row_count,
                SUM(CASE WHEN {qcol} IS NULL THEN 1 ELSE 0 END) AS null_count,
                {empty_sql} AS empty_count,
                {placeholder_sql} AS placeholder_count,
                {distinct_sql} AS distinct_count,
                {min_sql} AS min_value,
                {max_sql} AS max_value,
                {avg_sql} AS avg_value
            FROM {table_sql}
        """
        row = conn.query(stats_sql)[0]
        null_count = int(row["null_count"] or 0)
        empty_count = int(row["empty_count"] or 0)
        placeholder_count = int(row["placeholder_count"] or 0)
        non_empty_count = max(0, row_count - null_count - empty_count)
        distinct_count = row["distinct_count"]
        fill_rate = None if row_count == 0 else (row_count - null_count - empty_count) / row_count
        valid_rate = None if row_count == 0 else (row_count - null_count - empty_count - placeholder_count) / row_count
        duplicate_rate = None
        if distinct_count is not None and non_empty_count > 0:
            duplicate_rate = max(0.0, min(1.0, 1 - (int(distinct_count) / non_empty_count)))
        skipped_reason = None
        if distinct_skipped:
            skipped_reason = "distinct_skipped_row_limit"
        if sensitive_skip:
            skipped_reason = "sensitive_skip"
        return {
            "snapshot_id": snapshot_id,
            "column_id": column_row["column_id"],
            "table_id": table_id,
            "row_count": row_count,
            "null_count": null_count,
            "empty_count": empty_count,
            "placeholder_count": placeholder_count,
            "non_empty_count": non_empty_count,
            "distinct_count": int(distinct_count) if distinct_count is not None else None,
            "fill_rate": fill_rate,
            "valid_rate": valid_rate,
            "duplicate_rate": duplicate_rate,
            "min_value": row["min_value"],
            "max_value": row["max_value"],
            "avg_value": row["avg_value"],
            "p50_value": None,
            "is_estimated": False,
            "skipped_reason": skipped_reason,
        }

    def _scan_value_dist_and_samples(self, conn, snapshot_id: str, table_id: str, table: TableRef) -> None:
        columns = self.store.query(
            """
            SELECT mc.*, cs.row_count, cs.distinct_count
            FROM meta_column mc
            LEFT JOIN column_stat cs ON cs.snapshot_id = mc.snapshot_id AND cs.column_id = mc.column_id
            WHERE mc.snapshot_id = ? AND mc.table_id = ?
            ORDER BY mc.ordinal_position
            """,
            [snapshot_id, table_id],
        )
        dist_rows: list[dict[str, Any]] = []
        sample_rows: list[dict[str, Any]] = []
        for column in columns:
            if column.get("sensitive_action") == "skip":
                continue
            qcol = conn.quote(column["column_name"])
            table_sql = conn.table_sql(table)
            row_count = int(column.get("row_count") or 0)
            distinct_count = column.get("distinct_count")
            is_masked = column.get("sensitive_action") == "mask"
            if row_count and distinct_count is not None and int(distinct_count) <= self.settings.low_cardinality_limit:
                grouped: dict[str, int] = {}
                rows = conn.query(
                    f"""
                    SELECT {qcol} AS value, COUNT(*) AS value_count
                    FROM {table_sql}
                    GROUP BY {qcol}
                    ORDER BY value_count DESC
                    LIMIT {self.settings.low_cardinality_limit}
                    """
                )
                for row in rows:
                    label = mask_value(row["value"]) if is_masked else sentinel_label(row["value"])
                    grouped[label] = grouped.get(label, 0) + int(row["value_count"] or 0)
                for label, count in sorted(grouped.items(), key=lambda item: item[1], reverse=True):
                    dist_rows.append(
                        {
                            "snapshot_id": snapshot_id,
                            "column_id": column["column_id"],
                            "value_label": label,
                            "value_count": count,
                            "ratio": count / row_count if row_count else None,
                            "is_masked": is_masked,
                        }
                    )
            if row_count:
                rows = conn.query(
                    f"""
                    SELECT {qcol} AS sample_value
                    FROM {table_sql}
                    WHERE {qcol} IS NOT NULL
                    ORDER BY {conn.random_order_sql()}
                    LIMIT {self.settings.sample_size}
                    """
                )
                for row in rows:
                    value = mask_value(row["sample_value"]) if is_masked else sentinel_label(row["sample_value"])
                    sample_rows.append(
                        {
                            "snapshot_id": snapshot_id,
                            "column_id": column["column_id"],
                            "sample_value": value,
                            "is_masked": is_masked,
                        }
                    )
        self.store.replace_rows("value_dist", "snapshot_id = ? AND column_id IN (SELECT column_id FROM meta_column WHERE table_id = ?)", [snapshot_id, table_id], dist_rows)
        self.store.replace_rows("sample_data", "snapshot_id = ? AND column_id IN (SELECT column_id FROM meta_column WHERE table_id = ?)", [snapshot_id, table_id], sample_rows)

    def _scan_relations(self, conn, snapshot_id: str) -> None:
        relations = self.store.query(
            """
            SELECT *
            FROM meta_relation
            WHERE snapshot_id = ?
            ORDER BY child_schema, child_table, constraint_name
            """,
            [snapshot_id],
        )
        stat_rows = []
        for relation in relations:
            stat_rows.append(self._relation_stats(conn, snapshot_id, relation))
        self.store.replace_rows("relation_stat", "snapshot_id = ?", [snapshot_id], stat_rows)

    def _relation_stats(self, conn, snapshot_id: str, relation: dict[str, Any]) -> dict[str, Any]:
        child_columns = self._json_list(relation["child_columns_json"])
        parent_columns = self._json_list(relation["parent_columns_json"])
        base_row = {
            "snapshot_id": snapshot_id,
            "relation_id": relation["relation_id"],
            "child_table_id": relation["child_table_id"],
            "parent_table_id": relation["parent_table_id"],
            "child_fk_non_empty_rows": None,
            "matched_rows": None,
            "orphan_rows": None,
            "match_rate": None,
            "orphan_rate": None,
            "orphan_samples_json": None,
            "skipped_reason": None,
        }
        if not child_columns or len(child_columns) != len(parent_columns):
            return {**base_row, "skipped_reason": "invalid_relation_columns"}
        child = TableRef(relation["child_schema"], relation["child_table"])
        parent = TableRef(relation["parent_schema"], relation["parent_table"])
        child_table_sql = conn.table_sql(child)
        parent_table_sql = conn.table_sql(parent)
        non_empty_sql = " AND ".join(f"c.{conn.quote(column)} IS NOT NULL" for column in child_columns)
        join_sql = " AND ".join(
            f"c.{conn.quote(child_column)} = p.{conn.quote(parent_column)}"
            for child_column, parent_column in zip(child_columns, parent_columns)
        )
        exists_sql = f"EXISTS (SELECT 1 FROM {parent_table_sql} AS p WHERE {join_sql})"
        try:
            row = conn.query(
                f"""
                SELECT
                    COUNT(*) AS child_fk_non_empty_rows,
                    SUM(CASE WHEN {exists_sql} THEN 1 ELSE 0 END) AS matched_rows
                FROM {child_table_sql} AS c
                WHERE {non_empty_sql}
                """
            )[0]
            child_fk_non_empty_rows = int(row["child_fk_non_empty_rows"] or 0)
            matched_rows = int(row["matched_rows"] or 0)
            orphan_rows = max(0, child_fk_non_empty_rows - matched_rows)
            match_rate = matched_rows / child_fk_non_empty_rows if child_fk_non_empty_rows else None
            orphan_rate = orphan_rows / child_fk_non_empty_rows if child_fk_non_empty_rows else None
            orphan_samples = self._orphan_samples(conn, relation, child_columns, child_table_sql, non_empty_sql, exists_sql)
            return {
                **base_row,
                "child_fk_non_empty_rows": child_fk_non_empty_rows,
                "matched_rows": matched_rows,
                "orphan_rows": orphan_rows,
                "match_rate": match_rate,
                "orphan_rate": orphan_rate,
                "orphan_samples_json": write_json(orphan_samples),
            }
        except Exception as exc:
            return {**base_row, "skipped_reason": f"relation_scan_failed: {exc}"}

    def _orphan_samples(
        self,
        conn,
        relation: dict[str, Any],
        child_columns: list[str],
        child_table_sql: str,
        non_empty_sql: str,
        exists_sql: str,
    ) -> list[dict[str, Any]]:
        column_rows = self.store.query(
            """
            SELECT column_name, sensitive_action
            FROM meta_column
            WHERE snapshot_id = ? AND table_id = ?
            """,
            [relation["snapshot_id"], relation["child_table_id"]],
        )
        child_column_set = set(child_columns)
        actions = {
            row["column_name"]: row.get("sensitive_action")
            for row in column_rows
            if row["column_name"] in child_column_set
        }
        if any(actions.get(column) == "skip" for column in child_columns):
            return []
        select_sql = ", ".join(f"c.{conn.quote(column)} AS fk_{idx}" for idx, column in enumerate(child_columns))
        group_sql = ", ".join(f"c.{conn.quote(column)}" for column in child_columns)
        rows = conn.query(
            f"""
            SELECT {select_sql}, COUNT(*) AS orphan_count
            FROM {child_table_sql} AS c
            WHERE {non_empty_sql}
              AND NOT ({exists_sql})
            GROUP BY {group_sql}
            ORDER BY orphan_count DESC
            LIMIT 20
            """
        )
        samples: list[dict[str, Any]] = []
        for row in rows:
            values = {}
            for idx, column in enumerate(child_columns):
                raw_value = row.get(f"fk_{idx}")
                values[column] = mask_value(raw_value) if actions.get(column) == "mask" else sentinel_label(raw_value)
            samples.append({"values": values, "count": int(row["orphan_count"] or 0)})
        return samples

    def _physical_relation_rows(
        self,
        conn,
        snapshot_id: str,
        table_id_by_key: dict[tuple[str, str], str],
    ) -> list[dict[str, Any]]:
        try:
            foreign_keys = conn.list_foreign_keys()
        except Exception:
            foreign_keys = []
        rows: list[dict[str, Any]] = []
        for fk in foreign_keys:
            child_key = (fk.child_schema, fk.child_table)
            parent_key = (fk.parent_schema, fk.parent_table)
            if child_key not in table_id_by_key or parent_key not in table_id_by_key:
                continue
            rows.append(
                {
                    "snapshot_id": snapshot_id,
                    "relation_id": self._relation_id(snapshot_id, fk),
                    "relation_type": "physical_fk",
                    "constraint_name": fk.constraint_name,
                    "child_table_id": table_id_by_key[child_key],
                    "parent_table_id": table_id_by_key[parent_key],
                    "child_schema": fk.child_schema,
                    "child_table": fk.child_table,
                    "child_columns_json": write_json(fk.child_columns),
                    "parent_schema": fk.parent_schema,
                    "parent_table": fk.parent_table,
                    "parent_columns_json": write_json(fk.parent_columns),
                    "compare_rule": "raw",
                }
            )
        return rows

    def _finalize(self, snapshot_id: str) -> None:
        tables = self.store.query("SELECT * FROM meta_table WHERE snapshot_id = ?", [snapshot_id])
        for table in tables:
            date_columns = self.store.query(
                """
                SELECT mc.column_name, cs.min_value, cs.max_value
                FROM meta_column mc
                JOIN column_stat cs ON cs.snapshot_id = mc.snapshot_id AND cs.column_id = mc.column_id
                WHERE mc.snapshot_id = ? AND mc.table_id = ?
                  AND (upper(mc.data_type) LIKE '%DATE%' OR upper(mc.data_type) LIKE '%TIME%')
                  AND cs.min_value IS NOT NULL AND cs.max_value IS NOT NULL
                ORDER BY
                  CASE
                    WHEN lower(mc.column_name) LIKE '%date%' THEN 0
                    WHEN mc.column_name LIKE '%日期%' THEN 0
                    ELSE 1
                  END,
                  mc.ordinal_position
                LIMIT 1
                """,
                [snapshot_id, table["table_id"]],
            )
            if date_columns:
                picked = date_columns[0]
                self.store.execute(
                    """
                    UPDATE table_stat
                    SET date_column = ?, min_date = ?, max_date = ?, computed_at = now()
                    WHERE snapshot_id = ? AND table_id = ?
                    """,
                    [picked["column_name"], picked["min_value"], picked["max_value"], snapshot_id, table["table_id"]],
                )

    def _update_table_averages(self, snapshot_id: str, table_id: str) -> None:
        self.store.execute(
            """
            UPDATE table_stat
            SET
                avg_fill_rate = (
                    SELECT AVG(fill_rate) FROM column_stat
                    WHERE snapshot_id = ? AND table_id = ? AND fill_rate IS NOT NULL
                ),
                avg_valid_rate = (
                    SELECT AVG(valid_rate) FROM column_stat
                    WHERE snapshot_id = ? AND table_id = ? AND valid_rate IS NOT NULL
                ),
                computed_at = now()
            WHERE snapshot_id = ? AND table_id = ?
            """,
            [snapshot_id, table_id, snapshot_id, table_id, snapshot_id, table_id],
        )

    def _finish_snapshot(self, snapshot_id: str) -> None:
        if not self._snapshot_exists(snapshot_id):
            return
        failed = self.store.scalar(
            "SELECT COUNT(*) FROM scan_task WHERE snapshot_id = ? AND status = 'failed'",
            [snapshot_id],
        )
        status = "partial" if failed else "done"
        self.store.execute(
            "UPDATE scan_snapshot SET status = ?, finished_at = now() WHERE snapshot_id = ?",
            [status, snapshot_id],
        )
        self.store.checkpoint()

    def _sensitive_patterns(self) -> list[tuple[re.Pattern[str], str, str | None]]:
        rows = self.store.query("SELECT pattern, action, reason FROM sensitive_config")
        return [(re.compile(row["pattern"], re.IGNORECASE), row["action"], row.get("reason")) for row in rows]

    def _sensitive_policy(
        self,
        column: ColumnRef,
        patterns: list[tuple[re.Pattern[str], str, str | None]],
    ) -> SensitivePolicy:
        target = " ".join(
            item
            for item in [column.column_name, column.column_comment or "", column.table_name]
            if item
        )
        for pattern, action, reason in patterns:
            if pattern.search(target):
                return SensitivePolicy(True, action, reason)
        return SensitivePolicy(False)

    def _table_id(self, snapshot_id: str, table: TableRef) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{snapshot_id}:{table.schema_name}.{table.table_name}"))

    def _column_id(self, snapshot_id: str, table: TableRef, column_name: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{snapshot_id}:{table.schema_name}.{table.table_name}.{column_name}"))

    def _relation_id(self, snapshot_id: str, fk: ForeignKeyRef) -> str:
        child_cols = ",".join(fk.child_columns)
        parent_cols = ",".join(fk.parent_columns)
        return str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{snapshot_id}:{fk.constraint_name}:{fk.child_schema}.{fk.child_table}({child_cols})"
                f"->{fk.parent_schema}.{fk.parent_table}({parent_cols})",
            )
        )

    def _key_columns(self, primary_key: str | None) -> list[str]:
        if not primary_key:
            return []
        try:
            parsed = json.loads(primary_key)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except json.JSONDecodeError:
            pass
        return [item.strip() for item in str(primary_key).split(",") if item.strip()]

    def _json_list(self, value: str | None) -> list[str]:
        if not value:
            return []
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        return [str(item) for item in parsed]

    def _snapshot_exists(self, snapshot_id: str) -> bool:
        return bool(self.store.scalar("SELECT 1 FROM scan_snapshot WHERE snapshot_id = ?", [snapshot_id]))

    def _should_wait(self, snapshot_id: str) -> bool:
        if not self._snapshot_exists(snapshot_id):
            return True
        with self._control_lock:
            paused = snapshot_id in self._paused
        if paused:
            self.store.execute("UPDATE scan_snapshot SET status = 'paused' WHERE snapshot_id = ?", [snapshot_id])
        return paused
