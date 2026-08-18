from __future__ import annotations

from typing import Any

from app.scanner import ScanManager
from app.settings import Settings
from app.storage import MetadataStore, write_json


class RecordingExecutor:
    def __init__(self) -> None:
        self.submissions: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []

    def submit(self, fn: Any, *args: Any, **kwargs: Any) -> None:
        self.submissions.append((fn, args, kwargs))


def make_manager(tmp_path):
    settings = Settings(
        metadata_path=tmp_path / "metadata.duckdb",
        report_dir=tmp_path / "reports",
        sensitive_config_path=tmp_path / "missing-sensitive.yaml",
    )
    store = MetadataStore(settings)
    manager = ScanManager(store, settings)
    manager.executor.shutdown(wait=False)
    manager.executor = RecordingExecutor()
    return manager, store


def insert_task(store: MetadataStore, snapshot_id: str, source_id: str, task_id: str, task_type: str, status: str, priority: int) -> None:
    store.execute(
        """
        INSERT INTO scan_task
        (task_id, snapshot_id, source_id, task_type, status, priority, weight, attempt, error_message, started_at, finished_at)
        VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?, now(), now())
        """,
        [
            task_id,
            snapshot_id,
            source_id,
            task_type,
            status,
            priority,
            "boom" if status == "failed" else None,
        ],
    )


def test_rerun_failed_requeues_failed_task_and_dependents(tmp_path):
    manager, store = make_manager(tmp_path)
    source_id = "source-1"
    snapshot_id = "snapshot-1"
    table_id = "table-1"
    try:
        store.execute(
            """
            INSERT INTO data_source (source_id, name, dialect, conn_uri, options_json)
            VALUES (?, 'demo', 'sqlite', 'demo.sqlite', '{}')
            """,
            [source_id],
        )
        store.execute(
            """
            INSERT INTO scan_snapshot
            (snapshot_id, source_id, status, scope_json, metric_def_version, started_at, finished_at)
            VALUES (?, ?, 'partial', ?, 'test-v1', now(), now())
            """,
            [snapshot_id, source_id, write_json({"tables": ["patient"]})],
        )
        insert_task(store, snapshot_id, source_id, "task-struct", "struct", "done", 0)
        insert_task(store, snapshot_id, source_id, "task-rowcount", f"rowcount:{table_id}", "failed", 1)
        insert_task(store, snapshot_id, source_id, "task-column", f"column:{table_id}", "done", 2)
        insert_task(store, snapshot_id, source_id, "task-dist", f"value_dist_sample:{table_id}", "done", 3)
        insert_task(store, snapshot_id, source_id, "task-relation", "relation", "done", 8)
        insert_task(store, snapshot_id, source_id, "task-finalize", "finalize", "done", 9)

        result = manager.rerun_failed(snapshot_id)

        assert result["failed_task_count"] == 1
        assert result["reset_task_count"] == 4
        rows = store.query("SELECT task_type, status, error_message, started_at, finished_at FROM scan_task ORDER BY priority")
        tasks = {row["task_type"]: row for row in rows}
        assert tasks["struct"]["status"] == "done"
        assert tasks["relation"]["status"] == "done"
        for task_type in [
            f"rowcount:{table_id}",
            f"column:{table_id}",
            f"value_dist_sample:{table_id}",
            "finalize",
        ]:
            assert tasks[task_type]["status"] == "ready"
            assert tasks[task_type]["error_message"] is None
            assert tasks[task_type]["started_at"] is None
            assert tasks[task_type]["finished_at"] is None

        snapshot = store.query("SELECT status, finished_at, error_message FROM scan_snapshot WHERE snapshot_id = ?", [snapshot_id])[0]
        assert snapshot["status"] == "running"
        assert snapshot["finished_at"] is None
        assert snapshot["error_message"] is None
        assert len(manager.executor.submissions) == 1
        _, args, _ = manager.executor.submissions[0]
        assert args[0] == snapshot_id
        assert args[2] == ["patient"]
    finally:
        store.close()
