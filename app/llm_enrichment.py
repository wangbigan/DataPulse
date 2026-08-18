from __future__ import annotations

import json
import re
import uuid
from typing import Any
from urllib import error, request

from .settings import Settings
from .storage import MetadataStore, new_id, write_json


PROMPT_VERSION = "llm-metadata-enrichment-v1"


class LlmConfigError(RuntimeError):
    pass


class OpenAICompatibleClient:
    def __init__(self, settings: Settings):
        if not settings.llm_api_key:
            raise LlmConfigError("DATAPULSE_LLM_API_KEY is required before calling the LLM enrichment API.")
        self.base_url = settings.llm_base_url.rstrip("/")
        self.api_key = settings.llm_api_key
        self.model = settings.llm_model
        self.timeout_seconds = settings.llm_timeout_seconds

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        try:
            data = self._post(payload)
        except RuntimeError as exc:
            if "HTTP 400" not in str(exc) or "response_format" not in str(exc):
                raise
            payload.pop("response_format", None)
            data = self._post(payload)
        content = data["choices"][0]["message"]["content"]
        return _loads_json_object(content)

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as resp:
                raw = resp.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"LLM request failed: HTTP {exc.code}: {detail}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"LLM request failed: {exc}") from exc

        return json.loads(raw)


class LlmMetadataEnhancer:
    def __init__(self, store: MetadataStore, settings: Settings):
        self.store = store
        self.settings = settings

    def enrich_snapshot(
        self,
        snapshot_id: str,
        apply_changes: bool = True,
        extra_instructions: str | None = None,
    ) -> dict[str, Any]:
        context = self._snapshot_context(snapshot_id)
        client = OpenAICompatibleClient(self.settings)
        messages = self._messages(context, extra_instructions)
        run_id = new_id()
        self.store.execute(
            """
            INSERT INTO llm_enrichment_run
            (run_id, snapshot_id, provider, model, prompt_version, status, apply_changes, request_json)
            VALUES (?, ?, ?, ?, ?, 'running', ?, ?)
            """,
            [
                run_id,
                snapshot_id,
                "openai_compatible",
                self.settings.llm_model,
                PROMPT_VERSION,
                apply_changes,
                write_json({"messages": messages}),
            ],
        )
        try:
            result = client.complete_json(messages)
            summary = self._persist_result(snapshot_id, run_id, result, apply_changes)
        except Exception as exc:
            self.store.execute(
                """
                UPDATE llm_enrichment_run
                SET status = 'failed', error_message = ?, finished_at = now()
                WHERE run_id = ?
                """,
                [str(exc), run_id],
            )
            self.store.checkpoint()
            raise

        self.store.execute(
            """
            UPDATE llm_enrichment_run
            SET status = 'done', response_json = ?, finished_at = now()
            WHERE run_id = ?
            """,
            [write_json(result), run_id],
        )
        self.store.add_audit(
            "llm_metadata_enriched",
            {"snapshot_id": snapshot_id, "run_id": run_id, "apply_changes": apply_changes, **summary},
        )
        return {"run_id": run_id, "snapshot_id": snapshot_id, "apply_changes": apply_changes, **summary}

    def _snapshot_context(self, snapshot_id: str) -> dict[str, Any]:
        snapshot_rows = self.store.query(
            """
            SELECT ss.snapshot_id, ss.status, ds.source_id, ds.name AS source_name, ds.dialect
            FROM scan_snapshot ss
            JOIN data_source ds ON ds.source_id = ss.source_id
            WHERE ss.snapshot_id = ?
            """,
            [snapshot_id],
        )
        if not snapshot_rows:
            raise ValueError("Snapshot does not exist.")
        tables = self.store.query(
            """
            SELECT mt.*, ts.row_count, ts.avg_fill_rate, ts.avg_valid_rate,
                   ts.pk_duplicate_rate, ts.data_duplicate_columns, ts.data_duplicate_rate
            FROM meta_table mt
            LEFT JOIN table_stat ts ON ts.snapshot_id = mt.snapshot_id AND ts.table_id = mt.table_id
            WHERE mt.snapshot_id = ?
            ORDER BY mt.schema_name, mt.table_name
            LIMIT ?
            """,
            [snapshot_id, self.settings.llm_max_tables_per_request],
        )
        columns = self.store.query(
            """
            SELECT mc.*, cs.fill_rate, cs.valid_rate, cs.distinct_count, cs.duplicate_rate,
                   cs.skipped_reason
            FROM meta_column mc
            LEFT JOIN column_stat cs ON cs.snapshot_id = mc.snapshot_id AND cs.column_id = mc.column_id
            WHERE mc.snapshot_id = ?
            ORDER BY mc.schema_name, mc.table_name, mc.ordinal_position
            """,
            [snapshot_id],
        )
        columns_by_table: dict[str, list[dict[str, Any]]] = {}
        for column in columns:
            columns_by_table.setdefault(column["table_id"], []).append(
                {
                    "column_name": column["column_name"],
                    "data_type": column["data_type"],
                    "is_nullable": bool(column["is_nullable"]),
                    "ordinal_position": int(column["ordinal_position"]),
                    "column_comment": column.get("column_comment"),
                    "is_sensitive": bool(column.get("is_sensitive")),
                    "sensitive_action": column.get("sensitive_action"),
                    "fill_rate": column.get("fill_rate"),
                    "valid_rate": column.get("valid_rate"),
                    "distinct_count": column.get("distinct_count"),
                    "duplicate_rate": column.get("duplicate_rate"),
                    "skipped_reason": column.get("skipped_reason"),
                }
            )
        table_items = []
        for table in tables:
            table_items.append(
                {
                    "schema_name": table["schema_name"],
                    "table_name": table["table_name"],
                    "table_comment": table.get("table_comment"),
                    "declared_primary_key": table.get("primary_key"),
                    "column_count": table.get("column_count"),
                    "row_count": table.get("row_count"),
                    "avg_fill_rate": table.get("avg_fill_rate"),
                    "avg_valid_rate": table.get("avg_valid_rate"),
                    "pk_duplicate_rate": table.get("pk_duplicate_rate"),
                    "data_duplicate_columns": table.get("data_duplicate_columns"),
                    "data_duplicate_rate": table.get("data_duplicate_rate"),
                    "columns": columns_by_table.get(table["table_id"], []),
                }
            )
        return {**snapshot_rows[0], "tables": table_items}

    def _messages(self, context: dict[str, Any], extra_instructions: str | None) -> list[dict[str, str]]:
        instruction = {
            "required_json_shape": {
                "tables": [
                    {
                        "schema_name": "existing schema name",
                        "table_name": "existing table name",
                        "business_name": "short business name",
                        "business_description": "business meaning of the table",
                        "business_domain": "domain label such as patient, inpatient, outpatient, lab, exam, fee, drug, dictionary, platform, clinical",
                        "logical_primary_key": ["existing column name"],
                        "attribute_key_columns": ["existing column name"],
                        "confidence": 0.0,
                        "reason": "brief evidence",
                        "columns": [
                            {
                                "column_name": "existing column name",
                                "business_name": "short business name",
                                "business_description": "business meaning of the column",
                                "is_dictionary": False,
                                "dictionary_name": None,
                                "is_sensitive": False,
                                "sensitive_action": None,
                                "sensitive_reason": None,
                                "is_attribute_key": False,
                                "attribute_key_group": "llm",
                                "confidence": 0.0,
                                "reason": "brief evidence",
                            }
                        ],
                    }
                ],
                "relations": [
                    {
                        "child_schema": "existing schema name",
                        "child_table": "existing child table name",
                        "child_columns": ["existing child column name"],
                        "parent_schema": "existing schema name",
                        "parent_table": "existing parent table name",
                        "parent_columns": ["existing parent column name"],
                        "confidence": 0.0,
                        "reason": "brief evidence",
                    }
                ],
            }
        }
        user_payload = {
            "task": (
                "Enrich scanned metadata. Infer table and column business meanings, business domains, "
                "logical primary keys, logical foreign keys, dictionary-like columns, sensitive columns, "
                "and attribute identity columns for duplicate-rate calculation. Use only the supplied "
                "tables and columns. Do not invent identifiers."
            ),
            "rules": [
                "Return JSON only.",
                "Use confidence from 0 to 1.",
                "Only suggest relations when child and parent column counts match.",
                "Prefer skip for highly sensitive direct identifiers; prefer mask for contact or display identifiers.",
                "Do not include any raw data values in the answer.",
            ],
            "extra_instructions": extra_instructions or "",
            "output_contract": instruction,
            "metadata": context,
        }
        return [
            {
                "role": "system",
                "content": "You are a careful enterprise data catalog metadata steward. You return strict JSON.",
            },
            {"role": "user", "content": write_json(user_payload)},
        ]

    def _persist_result(
        self,
        snapshot_id: str,
        run_id: str,
        result: dict[str, Any],
        apply_changes: bool,
    ) -> dict[str, Any]:
        source_id = self.store.scalar("SELECT source_id FROM scan_snapshot WHERE snapshot_id = ?", [snapshot_id])
        table_rows = self.store.query("SELECT * FROM meta_table WHERE snapshot_id = ?", [snapshot_id])
        column_rows = self.store.query("SELECT * FROM meta_column WHERE snapshot_id = ?", [snapshot_id])
        tables_by_key = {_table_key(row["schema_name"], row["table_name"]): row for row in table_rows}
        tables_by_name = _unique_by_name(table_rows, "table_name")
        columns_by_table: dict[str, dict[str, dict[str, Any]]] = {}
        for row in column_rows:
            columns_by_table.setdefault(row["table_id"], {})[row["column_name"].lower()] = row

        table_annotations = []
        column_annotations = []
        relation_suggestions = []
        attribute_keys: list[tuple[dict[str, Any], list[str]]] = []
        logical_pk_updates: list[tuple[dict[str, Any], list[str]]] = []
        sensitive_updates: list[dict[str, Any]] = []
        logical_relations = []

        for item in _as_list(result.get("tables")):
            if not isinstance(item, dict):
                continue
            table = self._resolve_table(item, tables_by_key, tables_by_name)
            if not table:
                continue
            column_map = columns_by_table.get(table["table_id"], {})
            logical_pk = _valid_columns(item.get("logical_primary_key"), column_map)
            attribute_columns = _valid_columns(item.get("attribute_key_columns"), column_map)
            if logical_pk:
                logical_pk_updates.append((table, logical_pk))
            if attribute_columns:
                attribute_keys.append((table, attribute_columns))
            table_annotations.append(
                {
                    "snapshot_id": snapshot_id,
                    "table_id": table["table_id"],
                    "run_id": run_id,
                    "business_name": _optional_str(item.get("business_name")),
                    "business_description": _optional_str(item.get("business_description")),
                    "business_domain": _optional_str(item.get("business_domain")),
                    "logical_primary_key_json": write_json(logical_pk) if logical_pk else None,
                    "confidence": _confidence(item.get("confidence")),
                    "reason": _optional_str(item.get("reason")),
                }
            )
            for column_item in _as_list(item.get("columns")):
                if not isinstance(column_item, dict):
                    continue
                column_name = _optional_str(column_item.get("column_name"))
                if not column_name:
                    continue
                column = column_map.get(column_name.lower())
                if not column:
                    continue
                is_sensitive = _optional_bool(column_item.get("is_sensitive"))
                if is_sensitive:
                    sensitive_updates.append(
                        {
                            "column_id": column["column_id"],
                            "action": _sensitive_action(column_item.get("sensitive_action")),
                            "reason": _optional_str(column_item.get("sensitive_reason")) or _optional_str(column_item.get("reason")),
                        }
                    )
                if _optional_bool(column_item.get("is_attribute_key")):
                    attribute_keys.append((table, [column["column_name"]]))
                column_annotations.append(
                    {
                        "snapshot_id": snapshot_id,
                        "column_id": column["column_id"],
                        "table_id": table["table_id"],
                        "run_id": run_id,
                        "business_name": _optional_str(column_item.get("business_name")),
                        "business_description": _optional_str(column_item.get("business_description")),
                        "is_dictionary": _optional_bool(column_item.get("is_dictionary")),
                        "dictionary_name": _optional_str(column_item.get("dictionary_name")),
                        "is_sensitive": is_sensitive,
                        "sensitive_action": _sensitive_action(column_item.get("sensitive_action")) if is_sensitive else None,
                        "sensitive_reason": _optional_str(column_item.get("sensitive_reason")),
                        "is_attribute_key": _optional_bool(column_item.get("is_attribute_key")),
                        "attribute_key_group": _optional_str(column_item.get("attribute_key_group")) or ("llm" if _optional_bool(column_item.get("is_attribute_key")) else None),
                        "confidence": _confidence(column_item.get("confidence")),
                        "reason": _optional_str(column_item.get("reason")),
                    }
                )

        for item in _as_list(result.get("relations")):
            if not isinstance(item, dict):
                continue
            relation = self._validated_relation(item, tables_by_key, tables_by_name, columns_by_table)
            if not relation:
                continue
            relation_id = _relation_id(snapshot_id, relation)
            suggestion = {
                "snapshot_id": snapshot_id,
                "relation_id": relation_id,
                "run_id": run_id,
                "child_table_id": relation["child_table_id"],
                "parent_table_id": relation["parent_table_id"],
                "child_columns_json": write_json(relation["child_columns"]),
                "parent_columns_json": write_json(relation["parent_columns"]),
                "confidence": _confidence(item.get("confidence")),
                "reason": _optional_str(item.get("reason")),
                "status": "applied" if apply_changes else "suggested",
            }
            relation_suggestions.append(suggestion)
            logical_relations.append((relation_id, relation, item))

        self.store.replace_rows("llm_table_annotation", "snapshot_id = ? AND run_id = ?", [snapshot_id, run_id], table_annotations)
        self.store.replace_rows("llm_column_annotation", "snapshot_id = ? AND run_id = ?", [snapshot_id, run_id], column_annotations)
        self.store.replace_rows("llm_relation_suggestion", "snapshot_id = ? AND run_id = ?", [snapshot_id, run_id], relation_suggestions)

        if apply_changes:
            self._apply_changes(
                snapshot_id,
                str(source_id),
                logical_pk_updates,
                sensitive_updates,
                attribute_keys,
                logical_relations,
            )
        return {
            "table_annotations": len(table_annotations),
            "column_annotations": len(column_annotations),
            "relation_suggestions": len(relation_suggestions),
            "sensitive_updates": len(sensitive_updates) if apply_changes else 0,
            "attribute_key_updates": len(attribute_keys) if apply_changes else 0,
            "logical_relation_updates": len(logical_relations) if apply_changes else 0,
        }

    def _apply_changes(
        self,
        snapshot_id: str,
        source_id: str,
        logical_pk_updates: list[tuple[dict[str, Any], list[str]]],
        sensitive_updates: list[dict[str, Any]],
        attribute_keys: list[tuple[dict[str, Any], list[str]]],
        logical_relations: list[tuple[str, dict[str, Any], dict[str, Any]]],
    ) -> None:
        for table, columns in logical_pk_updates:
            if table.get("primary_key"):
                continue
            self.store.execute(
                "UPDATE meta_table SET primary_key = ? WHERE snapshot_id = ? AND table_id = ? AND primary_key IS NULL",
                [write_json(columns), snapshot_id, table["table_id"]],
            )
        for item in sensitive_updates:
            self.store.execute(
                """
                UPDATE meta_column
                SET is_sensitive = true,
                    sensitive_action = COALESCE(sensitive_action, ?),
                    sensitive_reason = COALESCE(sensitive_reason, ?)
                WHERE snapshot_id = ? AND column_id = ?
                """,
                [item["action"], item.get("reason"), snapshot_id, item["column_id"]],
            )
            self.store.execute("DELETE FROM sample_data WHERE snapshot_id = ? AND column_id = ?", [snapshot_id, item["column_id"]])
            self.store.execute("DELETE FROM value_dist WHERE snapshot_id = ? AND column_id = ?", [snapshot_id, item["column_id"]])

        tables_with_llm_keys = {(table["schema_name"], table["table_name"]) for table, _ in attribute_keys}
        for schema_name, table_name in tables_with_llm_keys:
            self.store.execute(
                """
                DELETE FROM attribute_key_config
                WHERE source_id = ? AND schema_name = ? AND table_name = ? AND key_group = 'llm'
                """,
                [source_id, schema_name, table_name],
            )
        seen_key_rows: set[tuple[str, str, str, str]] = set()
        for table, columns in attribute_keys:
            if not columns:
                continue
            for idx, column in enumerate(columns):
                key = (table["schema_name"], table["table_name"], "llm", column)
                if key in seen_key_rows:
                    continue
                seen_key_rows.add(key)
                self.store.execute(
                    """
                    INSERT INTO attribute_key_config
                    (source_id, schema_name, table_name, column_name, key_group, ordinal_position)
                    VALUES (?, ?, ?, ?, 'llm', ?)
                    """,
                    [source_id, table["schema_name"], table["table_name"], column, idx],
                )

        old_llm_relations = self.store.query(
            """
            SELECT relation_id
            FROM meta_relation
            WHERE snapshot_id = ? AND relation_type = 'logical_fk' AND constraint_name LIKE 'llm:%'
            """,
            [snapshot_id],
        )
        for row in old_llm_relations:
            self.store.execute("DELETE FROM relation_stat WHERE snapshot_id = ? AND relation_id = ?", [snapshot_id, row["relation_id"]])
        self.store.execute(
            """
            DELETE FROM meta_relation
            WHERE snapshot_id = ? AND relation_type = 'logical_fk' AND constraint_name LIKE 'llm:%'
            """,
            [snapshot_id],
        )
        existing_keys = {
            (
                row["child_table_id"],
                row["parent_table_id"],
                row["child_columns_json"],
                row["parent_columns_json"],
            )
            for row in self.store.query("SELECT * FROM meta_relation WHERE snapshot_id = ?", [snapshot_id])
        }
        for relation_id, relation, _ in logical_relations:
            key = (
                relation["child_table_id"],
                relation["parent_table_id"],
                write_json(relation["child_columns"]),
                write_json(relation["parent_columns"]),
            )
            if key in existing_keys:
                continue
            existing_keys.add(key)
            self.store.execute(
                """
                INSERT INTO meta_relation
                (snapshot_id, relation_id, relation_type, constraint_name,
                 child_table_id, parent_table_id, child_schema, child_table, child_columns_json,
                 parent_schema, parent_table, parent_columns_json, compare_rule)
                VALUES (?, ?, 'logical_fk', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'raw')
                """,
                [
                    snapshot_id,
                    relation_id,
                    f"llm:{relation['child_table']}->{relation['parent_table']}",
                    relation["child_table_id"],
                    relation["parent_table_id"],
                    relation["child_schema"],
                    relation["child_table"],
                    write_json(relation["child_columns"]),
                    relation["parent_schema"],
                    relation["parent_table"],
                    write_json(relation["parent_columns"]),
                ],
            )

    def _resolve_table(
        self,
        item: dict[str, Any],
        tables_by_key: dict[tuple[str, str], dict[str, Any]],
        tables_by_name: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        schema_name = _optional_str(item.get("schema_name")) or "main"
        table_name = _optional_str(item.get("table_name"))
        if not table_name:
            return None
        return tables_by_key.get(_table_key(schema_name, table_name)) or tables_by_name.get(table_name.lower())

    def _validated_relation(
        self,
        item: dict[str, Any],
        tables_by_key: dict[tuple[str, str], dict[str, Any]],
        tables_by_name: dict[str, dict[str, Any]],
        columns_by_table: dict[str, dict[str, dict[str, Any]]],
    ) -> dict[str, Any] | None:
        child = self._resolve_table(
            {"schema_name": item.get("child_schema"), "table_name": item.get("child_table")},
            tables_by_key,
            tables_by_name,
        )
        parent = self._resolve_table(
            {"schema_name": item.get("parent_schema"), "table_name": item.get("parent_table")},
            tables_by_key,
            tables_by_name,
        )
        if not child or not parent or child["table_id"] == parent["table_id"]:
            return None
        child_columns = _valid_columns(item.get("child_columns"), columns_by_table.get(child["table_id"], {}))
        parent_columns = _valid_columns(item.get("parent_columns"), columns_by_table.get(parent["table_id"], {}))
        if not child_columns or len(child_columns) != len(parent_columns):
            return None
        return {
            "child_table_id": child["table_id"],
            "parent_table_id": parent["table_id"],
            "child_schema": child["schema_name"],
            "child_table": child["table_name"],
            "child_columns": child_columns,
            "parent_schema": parent["schema_name"],
            "parent_table": parent["table_name"],
            "parent_columns": parent_columns,
        }


def _loads_json_object(content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("LLM response must be a JSON object.")
    return parsed


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _confidence(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return None


def _sensitive_action(value: Any) -> str:
    text = (_optional_str(value) or "skip").lower()
    return text if text in {"skip", "mask"} else "skip"


def _table_key(schema_name: str, table_name: str) -> tuple[str, str]:
    return (schema_name.lower(), table_name.lower())


def _unique_by_name(rows: list[dict[str, Any]], name_field: str) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row[name_field]).lower(), []).append(row)
    return {name: items[0] for name, items in grouped.items() if len(items) == 1}


def _valid_columns(value: Any, column_map: dict[str, dict[str, Any]]) -> list[str]:
    columns = []
    for item in _as_list(value):
        name = _optional_str(item)
        if not name:
            continue
        column = column_map.get(name.lower())
        if column:
            columns.append(column["column_name"])
    return columns


def _relation_id(snapshot_id: str, relation: dict[str, Any]) -> str:
    child_cols = ",".join(relation["child_columns"])
    parent_cols = ",".join(relation["parent_columns"])
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{snapshot_id}:logical:{relation['child_schema']}.{relation['child_table']}({child_cols})"
            f"->{relation['parent_schema']}.{relation['parent_table']}({parent_cols})",
        )
    )
