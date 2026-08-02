from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .datasource import open_source
from .report import generate_docx_report
from .scanner import ScanManager
from .settings import ROOT_DIR, get_settings
from .storage import MetadataStore, new_id, write_json


settings = get_settings()
store = MetadataStore(settings)
scanner = ScanManager(store, settings)
app = FastAPI(title=settings.app_name)


class SourceIn(BaseModel):
    name: str = Field(min_length=1)
    dialect: str = Field(pattern="^(sqlite|duckdb|postgres|postgresql|mysql|oracle|mssql)$")
    conn_uri: str = Field(min_length=1)
    options: dict[str, Any] = Field(default_factory=dict)


class ScanIn(BaseModel):
    source_id: str
    tables: list[str] = Field(default_factory=list)


def ok(data: Any) -> JSONResponse:
    return JSONResponse(jsonable_encoder(data))


@app.on_event("shutdown")
def shutdown() -> None:
    store.close()


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "metadata_path": str(settings.metadata_path)}


@app.get("/api/sources")
def list_sources() -> JSONResponse:
    rows = store.query("SELECT * FROM data_source ORDER BY created_at DESC")
    return ok(rows)


@app.post("/api/sources")
def create_source(payload: SourceIn) -> JSONResponse:
    source_id = new_id()
    store.execute(
        """
        INSERT INTO data_source (source_id, name, dialect, conn_uri, options_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        [source_id, payload.name, payload.dialect.lower(), payload.conn_uri, write_json(payload.options)],
    )
    return ok({"source_id": source_id})


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: str) -> JSONResponse:
    source = store.query("SELECT source_id, name FROM data_source WHERE source_id = ?", [source_id])
    if not source:
        raise HTTPException(status_code=404, detail="数据源不存在。")
    snapshot_count = store.scalar("SELECT COUNT(*) FROM scan_snapshot WHERE source_id = ?", [source_id]) or 0
    if int(snapshot_count) > 0:
        raise HTTPException(status_code=400, detail=f"该数据源已有 {snapshot_count} 个快照，不能删除。")
    store.execute("DELETE FROM data_source WHERE source_id = ?", [source_id])
    store.checkpoint()
    store.add_audit("data_source_deleted", {"source_id": source_id, "name": source[0]["name"]})
    return ok({"ok": True, "deleted": True})


@app.post("/api/sources/test")
def test_source(payload: SourceIn) -> JSONResponse:
    try:
        conn = open_source(payload.dialect, payload.conn_uri)
        tables = conn.list_tables()
        conn.close()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ok({"ok": True, "table_count": len(tables), "tables": [t.table_name for t in tables[:20]]})


@app.post("/api/scans")
def start_scan(payload: ScanIn) -> JSONResponse:
    try:
        snapshot_id = scanner.create_snapshot(payload.source_id, payload.tables)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ok({"snapshot_id": snapshot_id})


@app.get("/api/snapshots")
def list_snapshots() -> JSONResponse:
    rows = store.query(
        """
        SELECT ss.*, ds.name AS source_name, ds.dialect
        FROM scan_snapshot ss
        JOIN data_source ds ON ds.source_id = ss.source_id
        ORDER BY COALESCE(ss.started_at, ss.finished_at) DESC NULLS LAST
        """
    )
    for row in rows:
        row["progress"] = scanner.progress(row["snapshot_id"])["progress"]
    return ok(rows)


@app.get("/api/snapshots/latest")
def latest_snapshot() -> JSONResponse:
    rows = store.query(
        """
        SELECT ss.*, ds.name AS source_name, ds.dialect
        FROM scan_snapshot ss
        JOIN data_source ds ON ds.source_id = ss.source_id
        ORDER BY COALESCE(ss.finished_at, ss.started_at) DESC NULLS LAST
        LIMIT 1
        """
    )
    return ok(rows[0] if rows else None)


@app.get("/api/scans/{snapshot_id}/progress")
def scan_progress(snapshot_id: str) -> JSONResponse:
    return ok(scanner.progress(snapshot_id))


@app.get("/api/snapshots/{snapshot_id}/tasks")
def snapshot_tasks(snapshot_id: str) -> JSONResponse:
    snapshot_rows = store.query(
        """
        SELECT ss.*, ds.name AS source_name, ds.dialect
        FROM scan_snapshot ss
        JOIN data_source ds ON ds.source_id = ss.source_id
        WHERE ss.snapshot_id = ?
        """,
        [snapshot_id],
    )
    if not snapshot_rows:
        raise HTTPException(status_code=404, detail="快照不存在。")
    tasks = store.query(
        """
        SELECT
          st.task_id,
          st.task_type,
          st.status,
          st.priority,
          st.weight,
          st.attempt,
          st.crash_count,
          st.error_message,
          st.started_at,
          st.finished_at,
          mt.schema_name AS table_schema,
          mt.table_name AS table_name
        FROM scan_task st
        LEFT JOIN meta_table mt
          ON mt.snapshot_id = st.snapshot_id
         AND mt.table_id = split_part(st.task_type, ':', 2)
         AND st.task_type LIKE '%:%'
        WHERE st.snapshot_id = ?
        ORDER BY st.priority, COALESCE(st.started_at, st.finished_at) NULLS LAST, st.task_type
        """,
        [snapshot_id],
    )
    for task in tasks:
        started_at = task.get("started_at")
        finished_at = task.get("finished_at")
        task["duration_ms"] = None
        if started_at and finished_at:
            task["duration_ms"] = int((finished_at - started_at).total_seconds() * 1000)
    return ok({"snapshot": snapshot_rows[0], "progress": scanner.progress(snapshot_id), "tasks": tasks})


@app.post("/api/scans/{snapshot_id}/pause")
def pause_scan(snapshot_id: str) -> JSONResponse:
    scanner.pause(snapshot_id)
    return ok({"ok": True})


@app.post("/api/scans/{snapshot_id}/resume")
def resume_scan(snapshot_id: str) -> JSONResponse:
    scanner.resume(snapshot_id)
    return ok({"ok": True})


@app.delete("/api/snapshots/{snapshot_id}")
def delete_snapshot(snapshot_id: str) -> JSONResponse:
    scanner.delete_snapshot(snapshot_id)
    return ok({"ok": True, "deleted": True})


def resolve_snapshot(snapshot_id: str | None) -> str:
    if snapshot_id:
        return snapshot_id
    latest = store.query(
        "SELECT snapshot_id FROM scan_snapshot ORDER BY COALESCE(finished_at, started_at) DESC NULLS LAST LIMIT 1"
    )
    if not latest:
        raise HTTPException(status_code=404, detail="没有可用快照。")
    return latest[0]["snapshot_id"]


@app.get("/api/dashboard")
def dashboard(snapshot_id: str | None = None) -> JSONResponse:
    sid = resolve_snapshot(snapshot_id)
    overview = store.query(
        """
        SELECT
          (SELECT COUNT(*) FROM meta_table WHERE snapshot_id = ?) AS table_count,
          (SELECT COALESCE(SUM(row_count), 0) FROM table_stat WHERE snapshot_id = ?) AS total_rows,
          (SELECT COUNT(*) FROM meta_column WHERE snapshot_id = ?) AS column_count,
          (SELECT AVG(avg_fill_rate) FROM table_stat WHERE snapshot_id = ?) AS avg_fill_rate,
          (SELECT AVG(avg_valid_rate) FROM table_stat WHERE snapshot_id = ?) AS avg_valid_rate,
          (SELECT COUNT(*) FROM meta_column WHERE snapshot_id = ? AND is_sensitive) AS sensitive_columns,
          (SELECT COUNT(*) FROM meta_relation WHERE snapshot_id = ? AND relation_type = 'physical_fk') AS physical_foreign_keys,
          (
            SELECT COUNT(DISTINCT child_table_id)
            FROM meta_relation
            WHERE snapshot_id = ? AND relation_type = 'physical_fk'
          ) AS physical_fk_tables,
          (
            SELECT CAST(COUNT(DISTINCT child_table_id) AS DOUBLE) / NULLIF((SELECT COUNT(*) FROM meta_table WHERE snapshot_id = ?), 0)
            FROM meta_relation
            WHERE snapshot_id = ? AND relation_type = 'physical_fk'
          ) AS physical_fk_coverage_rate,
          (
            SELECT SUM(match_rate * child_fk_non_empty_rows) / NULLIF(SUM(child_fk_non_empty_rows), 0)
            FROM relation_stat
            WHERE snapshot_id = ? AND match_rate IS NOT NULL
          ) AS relation_health_rate,
          (
            SELECT COUNT(*)
            FROM column_stat
            WHERE snapshot_id = ? AND distinct_count IS NOT NULL AND distinct_count <= ?
          ) AS dictionary_candidates
        """,
        [sid, sid, sid, sid, sid, sid, sid, sid, sid, sid, sid, sid, settings.low_cardinality_limit],
    )[0]
    top_tables = store.query(
        """
        SELECT mt.table_id, mt.table_name, mt.column_count, ts.row_count, ts.avg_fill_rate, ts.avg_valid_rate
        FROM meta_table mt
        LEFT JOIN table_stat ts ON ts.snapshot_id = mt.snapshot_id AND ts.table_id = mt.table_id
        WHERE mt.snapshot_id = ?
        ORDER BY ts.row_count DESC NULLS LAST, mt.table_name
        LIMIT 12
        """,
        [sid],
    )
    low_columns = store.query(
        """
        SELECT mc.table_id, mc.column_id, mc.table_name, mc.column_name, mc.data_type,
               cs.fill_rate, cs.valid_rate, cs.skipped_reason
        FROM column_stat cs
        JOIN meta_column mc ON mc.snapshot_id = cs.snapshot_id AND mc.column_id = cs.column_id
        WHERE cs.snapshot_id = ? AND cs.fill_rate IS NOT NULL
        ORDER BY cs.fill_rate ASC
        LIMIT 20
        """,
        [sid],
    )
    gap_columns = store.query(
        """
        SELECT mc.table_name, mc.column_name, cs.fill_rate, cs.valid_rate,
               (cs.fill_rate - cs.valid_rate) AS gap
        FROM column_stat cs
        JOIN meta_column mc ON mc.snapshot_id = cs.snapshot_id AND mc.column_id = cs.column_id
        WHERE cs.snapshot_id = ? AND cs.fill_rate IS NOT NULL AND cs.valid_rate IS NOT NULL
        ORDER BY gap DESC
        LIMIT 12
        """,
        [sid],
    )
    pk_duplicates = store.query(
        """
        SELECT mt.table_id, mt.table_name, mt.schema_name, mt.primary_key, ts.row_count,
               ts.pk_duplicate_rows, ts.pk_duplicate_rate, ts.pk_duplicate_skipped_reason
        FROM table_stat ts
        JOIN meta_table mt ON mt.snapshot_id = ts.snapshot_id AND mt.table_id = ts.table_id
        WHERE ts.snapshot_id = ? AND mt.primary_key IS NOT NULL
        ORDER BY ts.pk_duplicate_rate DESC NULLS LAST, ts.pk_duplicate_rows DESC NULLS LAST, mt.table_name
        LIMIT 12
        """,
        [sid],
    )
    relations = store.query(
        """
        SELECT mr.*, rs.child_fk_non_empty_rows, rs.matched_rows, rs.orphan_rows,
               rs.match_rate, rs.orphan_rate, rs.orphan_samples_json, rs.skipped_reason
        FROM meta_relation mr
        LEFT JOIN relation_stat rs ON rs.snapshot_id = mr.snapshot_id AND rs.relation_id = mr.relation_id
        WHERE mr.snapshot_id = ?
        ORDER BY rs.match_rate ASC NULLS LAST, rs.orphan_rows DESC NULLS LAST, mr.child_table, mr.parent_table
        LIMIT 20
        """,
        [sid],
    )
    return ok(
        {
            "snapshot_id": sid,
            "overview": overview,
            "top_tables": top_tables,
            "low_columns": low_columns,
            "gap_columns": gap_columns,
            "pk_duplicates": pk_duplicates,
            "relations": relations,
            "progress": scanner.progress(sid),
        }
    )


@app.get("/api/tables")
def tables(snapshot_id: str | None = None, q: str = "") -> JSONResponse:
    sid = resolve_snapshot(snapshot_id)
    like = f"%{q.lower()}%"
    rows = store.query(
        """
        SELECT mt.*, ts.row_count, ts.avg_fill_rate, ts.avg_valid_rate,
               ts.pk_duplicate_rows, ts.pk_duplicate_rate, ts.pk_duplicate_skipped_reason,
               ts.date_column, ts.min_date, ts.max_date
        FROM meta_table mt
        LEFT JOIN table_stat ts ON ts.snapshot_id = mt.snapshot_id AND ts.table_id = mt.table_id
        WHERE mt.snapshot_id = ?
          AND (? = '' OR lower(mt.table_name) LIKE ? OR lower(COALESCE(mt.table_comment, '')) LIKE ?)
        ORDER BY ts.avg_fill_rate ASC NULLS LAST, mt.table_name
        """,
        [sid, q, like, like],
    )
    return ok({"snapshot_id": sid, "tables": rows})


@app.get("/api/tables/{table_id}")
def table_detail(table_id: str, snapshot_id: str | None = None) -> JSONResponse:
    sid = resolve_snapshot(snapshot_id)
    table_rows = store.query(
        """
        SELECT mt.*, ts.row_count, ts.avg_fill_rate, ts.avg_valid_rate,
               ts.pk_duplicate_rows, ts.pk_duplicate_rate, ts.pk_duplicate_skipped_reason,
               ts.date_column, ts.min_date, ts.max_date
        FROM meta_table mt
        LEFT JOIN table_stat ts ON ts.snapshot_id = mt.snapshot_id AND ts.table_id = mt.table_id
        WHERE mt.snapshot_id = ? AND mt.table_id = ?
        """,
        [sid, table_id],
    )
    if not table_rows:
        raise HTTPException(status_code=404, detail="表不存在。")
    columns = store.query(
        """
        SELECT mc.*, cs.row_count, cs.null_count, cs.empty_count, cs.placeholder_count,
               cs.distinct_count, cs.fill_rate, cs.valid_rate, cs.duplicate_rate,
               cs.min_value, cs.max_value, cs.avg_value, cs.is_estimated, cs.skipped_reason,
               cs.computed_at
        FROM meta_column mc
        LEFT JOIN column_stat cs ON cs.snapshot_id = mc.snapshot_id AND cs.column_id = mc.column_id
        WHERE mc.snapshot_id = ? AND mc.table_id = ?
        ORDER BY mc.ordinal_position
        """,
        [sid, table_id],
    )
    relations = store.query(
        """
        SELECT mr.*, rs.child_fk_non_empty_rows, rs.matched_rows, rs.orphan_rows,
               rs.match_rate, rs.orphan_rate, rs.orphan_samples_json, rs.skipped_reason
        FROM meta_relation mr
        LEFT JOIN relation_stat rs ON rs.snapshot_id = mr.snapshot_id AND rs.relation_id = mr.relation_id
        WHERE mr.snapshot_id = ?
          AND (mr.child_table_id = ? OR mr.parent_table_id = ?)
        ORDER BY mr.child_table, mr.parent_table
        """,
        [sid, table_id, table_id],
    )
    return ok({"snapshot_id": sid, "table": table_rows[0], "columns": columns, "relations": relations})


@app.get("/api/relations")
def relations(snapshot_id: str | None = None) -> JSONResponse:
    sid = resolve_snapshot(snapshot_id)
    rows = store.query(
        """
        SELECT mr.*, rs.child_fk_non_empty_rows, rs.matched_rows, rs.orphan_rows,
               rs.match_rate, rs.orphan_rate, rs.orphan_samples_json, rs.skipped_reason
        FROM meta_relation mr
        LEFT JOIN relation_stat rs ON rs.snapshot_id = mr.snapshot_id AND rs.relation_id = mr.relation_id
        WHERE mr.snapshot_id = ?
        ORDER BY rs.match_rate ASC NULLS LAST, mr.child_table, mr.parent_table
        """,
        [sid],
    )
    return ok({"snapshot_id": sid, "relations": rows})


@app.get("/api/columns/{column_id}")
def column_detail(column_id: str, snapshot_id: str | None = None) -> JSONResponse:
    sid = resolve_snapshot(snapshot_id)
    column_rows = store.query(
        """
        SELECT mc.*, cs.*
        FROM meta_column mc
        LEFT JOIN column_stat cs ON cs.snapshot_id = mc.snapshot_id AND cs.column_id = mc.column_id
        WHERE mc.snapshot_id = ? AND mc.column_id = ?
        """,
        [sid, column_id],
    )
    if not column_rows:
        raise HTTPException(status_code=404, detail="字段不存在。")
    dist = store.query(
        """
        SELECT value_label, value_count, ratio, is_masked
        FROM value_dist
        WHERE snapshot_id = ? AND column_id = ?
        ORDER BY value_count DESC
        """,
        [sid, column_id],
    )
    samples = store.query(
        """
        SELECT sample_value, is_masked
        FROM sample_data
        WHERE snapshot_id = ? AND column_id = ?
        LIMIT 50
        """,
        [sid, column_id],
    )
    metrics = store.query(
        """
        SELECT metric_code, name, definition, formula, denominator, boundary
        FROM metric_registry
        WHERE version = (SELECT metric_def_version FROM scan_snapshot WHERE snapshot_id = ?)
        ORDER BY metric_code
        """,
        [sid],
    )
    return ok({"snapshot_id": sid, "column": column_rows[0], "value_dist": dist, "samples": samples, "metrics": metrics})


@app.get("/api/metrics")
def metrics(snapshot_id: str | None = None) -> JSONResponse:
    sid = resolve_snapshot(snapshot_id) if snapshot_id else None
    version = settings.metric_def_version
    if sid:
        version = store.scalar("SELECT metric_def_version FROM scan_snapshot WHERE snapshot_id = ?", [sid]) or version
    rows = store.query("SELECT * FROM metric_registry WHERE version = ? ORDER BY metric_code", [version])
    return ok(rows)


@app.get("/api/export-review/{snapshot_id}")
def export_review(snapshot_id: str) -> JSONResponse:
    counts = store.query(
        """
        SELECT
          (SELECT COUNT(*) FROM meta_table WHERE snapshot_id = ?) AS tables,
          (SELECT COUNT(*) FROM meta_column WHERE snapshot_id = ?) AS columns,
          (SELECT COUNT(*) FROM sample_data WHERE snapshot_id = ?) AS samples,
          (SELECT COUNT(*) FROM value_dist WHERE snapshot_id = ?) AS value_items,
          (SELECT COUNT(*) FROM meta_column WHERE snapshot_id = ? AND is_sensitive) AS sensitive_columns
        """,
        [snapshot_id, snapshot_id, snapshot_id, snapshot_id, snapshot_id],
    )[0]
    sensitive = store.query(
        """
        SELECT table_name, column_name, sensitive_action, sensitive_reason
        FROM meta_column
        WHERE snapshot_id = ? AND is_sensitive
        ORDER BY table_name, column_name
        """,
        [snapshot_id],
    )
    return ok({"snapshot_id": snapshot_id, "content": counts, "sensitive_items": sensitive})


@app.post("/api/samples/clear")
def clear_samples() -> JSONResponse:
    before = store.scalar("SELECT COUNT(*) FROM sample_data") or 0
    store.execute("DELETE FROM sample_data")
    store.checkpoint()
    store.add_audit("sample_data_cleared", {"deleted_rows": int(before), "checkpoint": True})
    return ok({"ok": True, "deleted_rows": int(before), "checkpoint": True})


@app.post("/api/reports/{snapshot_id}/docx")
def report_docx(snapshot_id: str) -> FileResponse:
    try:
        path = generate_docx_report(store, snapshot_id, settings.report_dir)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=Path(path).name,
    )


STATIC_DIR = ROOT_DIR / "app" / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
