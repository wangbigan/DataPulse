from __future__ import annotations

import re
import sqlite3
from urllib.parse import unquote, urlparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import duckdb


@dataclass(frozen=True)
class TableRef:
    schema_name: str
    table_name: str
    table_comment: str | None = None
    primary_key: str | None = None


@dataclass(frozen=True)
class ColumnRef:
    schema_name: str
    table_name: str
    column_name: str
    data_type: str
    is_nullable: bool
    ordinal_position: int
    column_comment: str | None = None


@dataclass(frozen=True)
class ForeignKeyRef:
    constraint_name: str
    child_schema: str
    child_table: str
    child_columns: list[str]
    parent_schema: str
    parent_table: str
    parent_columns: list[str]


class SourceConnection(Protocol):
    dialect: str

    def close(self) -> None: ...

    def list_tables(self) -> list[TableRef]: ...

    def list_columns(self, table: TableRef) -> list[ColumnRef]: ...

    def list_foreign_keys(self) -> list[ForeignKeyRef]: ...

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]: ...

    def quote(self, identifier: str) -> str: ...

    def table_sql(self, table: TableRef) -> str: ...

    def random_order_sql(self) -> str: ...

    def text_cast_sql(self, expression: str) -> str: ...

    def literal_sql(self, value: Any) -> str: ...


class SQLiteSource:
    dialect = "sqlite"

    def __init__(self, conn_uri: str):
        path = normalize_file_uri(conn_uri, "sqlite")
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self.conn.close()

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        cur = self.conn.execute(sql, params or [])
        return [dict(row) for row in cur.fetchall()]

    def list_tables(self) -> list[TableRef]:
        rows = self.query(
            """
            SELECT name
            FROM sqlite_master
            WHERE type IN ('table', 'view')
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
        tables: list[TableRef] = []
        for row in rows:
            columns = self.query(f"PRAGMA table_info({self.quote(row['name'])})")
            pks = [col["name"] for col in columns if col.get("pk")]
            tables.append(TableRef("main", row["name"], None, ", ".join(pks) or None))
        return tables

    def list_columns(self, table: TableRef) -> list[ColumnRef]:
        rows = self.query(f"PRAGMA table_info({self.quote(table.table_name)})")
        return [
            ColumnRef(
                schema_name=table.schema_name,
                table_name=table.table_name,
                column_name=row["name"],
                data_type=row["type"] or "TEXT",
                is_nullable=not bool(row["notnull"]),
                ordinal_position=int(row["cid"]) + 1,
                column_comment=None,
            )
            for row in rows
        ]

    def list_foreign_keys(self) -> list[ForeignKeyRef]:
        refs: list[ForeignKeyRef] = []
        tables = self.list_tables()
        pk_by_table = {
            table.table_name: [item.strip() for item in table.primary_key.split(",")]
            for table in tables
            if table.primary_key
        }
        for table in tables:
            rows = self.query(f"PRAGMA foreign_key_list({self.quote(table.table_name)})")
            grouped: dict[int, list[dict[str, Any]]] = {}
            for row in rows:
                grouped.setdefault(int(row["id"]), []).append(row)
            for fk_id, items in grouped.items():
                ordered = sorted(items, key=lambda item: int(item["seq"]))
                parent_table = str(ordered[0]["table"])
                parent_columns = [
                    str(item["to"]) if item.get("to") else pk_by_table.get(parent_table, [])[idx]
                    for idx, item in enumerate(ordered)
                    if item.get("to") or idx < len(pk_by_table.get(parent_table, []))
                ]
                if len(parent_columns) != len(ordered):
                    continue
                refs.append(
                    ForeignKeyRef(
                        constraint_name=f"fk_{table.table_name}_{fk_id}",
                        child_schema=table.schema_name,
                        child_table=table.table_name,
                        child_columns=[str(item["from"]) for item in ordered],
                        parent_schema="main",
                        parent_table=parent_table,
                        parent_columns=parent_columns,
                    )
                )
        return refs

    def quote(self, identifier: str) -> str:
        return quote_identifier(identifier)

    def table_sql(self, table: TableRef) -> str:
        return self.quote(table.table_name)

    def random_order_sql(self) -> str:
        return "RANDOM()"

    def text_cast_sql(self, expression: str) -> str:
        return f"CAST({expression} AS TEXT)"

    def literal_sql(self, value: Any) -> str:
        return standard_literal(value)


class DuckDBSource:
    dialect = "duckdb"

    def __init__(self, conn_uri: str):
        path = normalize_file_uri(conn_uri, "duckdb")
        self.path = path
        self.conn = duckdb.connect(path, read_only=True)

    def close(self) -> None:
        self.conn.close()

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        cur = self.conn.execute(sql, params or [])
        columns = [desc[0] for desc in cur.description or []]
        return [dict(zip(columns, row)) for row in cur.fetchall()]

    def list_tables(self) -> list[TableRef]:
        rows = self.query(
            """
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_schema NOT IN ('information_schema', 'pg_catalog')
              AND table_type IN ('BASE TABLE', 'VIEW')
            ORDER BY table_schema, table_name
            """
        )
        pks = self._primary_keys()
        return [
            TableRef(
                row["table_schema"],
                row["table_name"],
                primary_key=", ".join(pks.get((row["table_schema"], row["table_name"]), [])) or None,
            )
            for row in rows
        ]

    def list_columns(self, table: TableRef) -> list[ColumnRef]:
        rows = self.query(
            """
            SELECT table_schema, table_name, column_name, data_type, is_nullable, ordinal_position
            FROM information_schema.columns
            WHERE table_schema = ? AND table_name = ?
            ORDER BY ordinal_position
            """,
            [table.schema_name, table.table_name],
        )
        return [
            ColumnRef(
                schema_name=row["table_schema"],
                table_name=row["table_name"],
                column_name=row["column_name"],
                data_type=row["data_type"],
                is_nullable=str(row["is_nullable"]).upper() == "YES",
                ordinal_position=int(row["ordinal_position"]),
            )
            for row in rows
        ]

    def list_foreign_keys(self) -> list[ForeignKeyRef]:
        try:
            rows = self.query(
                """
                SELECT
                    kcu.constraint_name,
                    kcu.table_schema AS child_schema,
                    kcu.table_name AS child_table,
                    kcu.column_name AS child_column,
                    ccu.table_schema AS parent_schema,
                    ccu.table_name AS parent_table,
                    ccu.column_name AS parent_column,
                    kcu.ordinal_position
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_schema = kcu.constraint_schema
                 AND tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                 AND tc.table_name = kcu.table_name
                JOIN information_schema.constraint_column_usage ccu
                  ON tc.constraint_schema = ccu.constraint_schema
                 AND tc.constraint_name = ccu.constraint_name
                WHERE tc.constraint_type = 'FOREIGN KEY'
                ORDER BY child_schema, child_table, constraint_name, ordinal_position
                """
            )
        except Exception:
            return []
        return group_foreign_key_rows(rows)

    def _primary_keys(self) -> dict[tuple[str, str], list[str]]:
        try:
            rows = self.query(
                """
                SELECT
                    kcu.table_schema,
                    kcu.table_name,
                    kcu.column_name,
                    kcu.ordinal_position
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_schema = kcu.constraint_schema
                 AND tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                 AND tc.table_name = kcu.table_name
                WHERE tc.constraint_type = 'PRIMARY KEY'
                ORDER BY kcu.table_schema, kcu.table_name, kcu.ordinal_position
                """
            )
        except Exception:
            return {}
        pks: dict[tuple[str, str], list[str]] = {}
        for row in rows:
            pks.setdefault((row["table_schema"], row["table_name"]), []).append(row["column_name"])
        return pks

    def quote(self, identifier: str) -> str:
        return quote_identifier(identifier)

    def table_sql(self, table: TableRef) -> str:
        return f"{self.quote(table.schema_name)}.{self.quote(table.table_name)}"

    def random_order_sql(self) -> str:
        return "random()"

    def text_cast_sql(self, expression: str) -> str:
        return f"CAST({expression} AS VARCHAR)"

    def literal_sql(self, value: Any) -> str:
        return standard_literal(value)


class MySQLSource:
    dialect = "mysql"

    def __init__(self, conn_uri: str):
        try:
            import pymysql
            from pymysql.cursors import DictCursor
        except ImportError as exc:
            raise RuntimeError("PyMySQL is not installed. Run: python -m pip install PyMySQL") from exc
        cfg = parse_mysql_uri(conn_uri)
        self.database = cfg["database"]
        self.conn = pymysql.connect(
            host=cfg["host"],
            port=cfg["port"],
            user=cfg["user"],
            password=cfg["password"],
            database=cfg["database"],
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=True,
            connect_timeout=5,
            read_timeout=600,
            write_timeout=30,
        )

    def close(self) -> None:
        self.conn.close()

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params or [])
            return list(cur.fetchall())

    def list_tables(self) -> list[TableRef]:
        rows = self.query(
            """
            SELECT
                table_schema AS table_schema,
                table_name AS table_name,
                table_comment AS table_comment
            FROM information_schema.tables
            WHERE table_schema = DATABASE()
              AND table_type IN ('BASE TABLE', 'VIEW')
            ORDER BY table_name
            """
        )
        pk_rows = self.query(
            """
            SELECT
                table_schema AS table_schema,
                table_name AS table_name,
                column_name AS column_name,
                ordinal_position AS ordinal_position
            FROM information_schema.key_column_usage
            WHERE table_schema = DATABASE()
              AND constraint_name = 'PRIMARY'
            ORDER BY table_name, ordinal_position
            """
        )
        pks: dict[tuple[str, str], list[str]] = {}
        for row in pk_rows:
            pks.setdefault((row["table_schema"], row["table_name"]), []).append(row["column_name"])
        return [
            TableRef(
                row["table_schema"],
                row["table_name"],
                row.get("table_comment"),
                ", ".join(pks.get((row["table_schema"], row["table_name"]), [])) or None,
            )
            for row in rows
        ]

    def list_columns(self, table: TableRef) -> list[ColumnRef]:
        rows = self.query(
            """
            SELECT
                table_schema AS table_schema,
                table_name AS table_name,
                column_name AS column_name,
                column_type AS data_type,
                is_nullable AS is_nullable,
                ordinal_position AS ordinal_position,
                column_comment AS column_comment
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            """,
            [table.schema_name, table.table_name],
        )
        return [
            ColumnRef(
                schema_name=row["table_schema"],
                table_name=row["table_name"],
                column_name=row["column_name"],
                data_type=row["data_type"],
                is_nullable=str(row["is_nullable"]).upper() == "YES",
                ordinal_position=int(row["ordinal_position"]),
                column_comment=row.get("column_comment"),
            )
            for row in rows
        ]

    def list_foreign_keys(self) -> list[ForeignKeyRef]:
        rows = self.query(
            """
            SELECT
                constraint_name AS constraint_name,
                table_schema AS child_schema,
                table_name AS child_table,
                column_name AS child_column,
                referenced_table_schema AS parent_schema,
                referenced_table_name AS parent_table,
                referenced_column_name AS parent_column,
                ordinal_position AS ordinal_position
            FROM information_schema.key_column_usage
            WHERE table_schema = DATABASE()
              AND referenced_table_name IS NOT NULL
            ORDER BY table_name, constraint_name, ordinal_position
            """
        )
        return group_foreign_key_rows(rows)

    def quote(self, identifier: str) -> str:
        return "`" + identifier.replace("`", "``") + "`"

    def table_sql(self, table: TableRef) -> str:
        return f"{self.quote(table.schema_name)}.{self.quote(table.table_name)}"

    def random_order_sql(self) -> str:
        return "RAND()"

    def text_cast_sql(self, expression: str) -> str:
        return f"CAST({expression} AS CHAR)"

    def literal_sql(self, value: Any) -> str:
        return self.conn.escape(value)


class SQLAlchemySource:
    def __init__(self, dialect: str, conn_uri: str):
        try:
            from sqlalchemy import create_engine, inspect, text
        except ImportError as exc:
            raise RuntimeError("SQLAlchemy connectors are not installed. Install the connectors extra.") from exc
        self.dialect = dialect
        self._text = text
        self._inspect = inspect
        self.engine = create_engine(conn_uri, pool_pre_ping=True)

    def close(self) -> None:
        self.engine.dispose()

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            result = conn.execute(self._text(sql), params or {})
            return [dict(row._mapping) for row in result]

    def list_tables(self) -> list[TableRef]:
        rows = self.query(
            """
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_schema NOT IN ('information_schema', 'pg_catalog')
              AND table_type IN ('BASE TABLE', 'VIEW')
            ORDER BY table_schema, table_name
            """
        )
        inspector = self._inspect(self.engine)
        tables: list[TableRef] = []
        for row in rows:
            pk = None
            try:
                pk_columns = inspector.get_pk_constraint(row["table_name"], schema=row["table_schema"]).get(
                    "constrained_columns",
                    [],
                )
                pk = ", ".join(pk_columns) or None
            except Exception:
                pk = None
            tables.append(TableRef(row["table_schema"], row["table_name"], primary_key=pk))
        return tables

    def list_columns(self, table: TableRef) -> list[ColumnRef]:
        rows = self.query(
            f"""
            SELECT table_schema, table_name, column_name, data_type, is_nullable, ordinal_position
            FROM information_schema.columns
            WHERE table_schema = '{escape_literal(table.schema_name)}'
              AND table_name = '{escape_literal(table.table_name)}'
            ORDER BY ordinal_position
            """
        )
        return [
            ColumnRef(
                schema_name=row["table_schema"],
                table_name=row["table_name"],
                column_name=row["column_name"],
                data_type=row["data_type"],
                is_nullable=str(row["is_nullable"]).upper() in {"YES", "TRUE", "1"},
                ordinal_position=int(row["ordinal_position"]),
            )
            for row in rows
        ]

    def list_foreign_keys(self) -> list[ForeignKeyRef]:
        inspector = self._inspect(self.engine)
        refs: list[ForeignKeyRef] = []
        for table in self.list_tables():
            try:
                foreign_keys = inspector.get_foreign_keys(table.table_name, schema=table.schema_name)
            except Exception:
                continue
            for item in foreign_keys:
                child_columns = list(item.get("constrained_columns") or [])
                parent_columns = list(item.get("referred_columns") or [])
                parent_table = item.get("referred_table")
                if not child_columns or not parent_columns or not parent_table:
                    continue
                refs.append(
                    ForeignKeyRef(
                        constraint_name=item.get("name") or f"fk_{table.table_name}_{'_'.join(child_columns)}",
                        child_schema=table.schema_name,
                        child_table=table.table_name,
                        child_columns=child_columns,
                        parent_schema=item.get("referred_schema") or table.schema_name,
                        parent_table=parent_table,
                        parent_columns=parent_columns,
                    )
                )
        return refs

    def quote(self, identifier: str) -> str:
        return quote_identifier(identifier)

    def table_sql(self, table: TableRef) -> str:
        return f"{self.quote(table.schema_name)}.{self.quote(table.table_name)}"

    def random_order_sql(self) -> str:
        return "random()"

    def text_cast_sql(self, expression: str) -> str:
        return f"CAST({expression} AS VARCHAR)"

    def literal_sql(self, value: Any) -> str:
        return standard_literal(value)


def open_source(dialect: str, conn_uri: str) -> SourceConnection:
    dialect = dialect.lower()
    if dialect == "sqlite":
        return SQLiteSource(conn_uri)
    if dialect == "duckdb":
        return DuckDBSource(conn_uri)
    if dialect == "mysql":
        return MySQLSource(conn_uri)
    if dialect in {"postgres", "postgresql", "oracle", "mssql"}:
        return SQLAlchemySource(dialect, conn_uri)
    raise ValueError(f"Unsupported source dialect: {dialect}")


def normalize_file_uri(conn_uri: str, dialect: str) -> str:
    prefix = f"{dialect}:///"
    if conn_uri.startswith(prefix):
        return conn_uri[len(prefix) :]
    if conn_uri.startswith("file://"):
        return conn_uri[len("file://") :]
    return str(Path(conn_uri))


def parse_mysql_uri(conn_uri: str) -> dict[str, Any]:
    parsed = urlparse(conn_uri)
    if parsed.scheme not in {"mysql", "mysql+pymysql"}:
        raise ValueError("MySQL URI should look like mysql://user:password@127.0.0.1:3306/database")
    database = parsed.path.lstrip("/")
    if not database:
        raise ValueError("MySQL URI must include a database name.")
    return {
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or "root"),
        "password": unquote(parsed.password or ""),
        "database": unquote(database),
    }


def quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def escape_literal(value: str) -> str:
    return value.replace("'", "''")


def standard_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def group_foreign_key_rows(rows: list[dict[str, Any]]) -> list[ForeignKeyRef]:
    grouped: dict[tuple[str, str, str, str, str, str], list[dict[str, Any]]] = {}
    for raw_row in rows:
        row = {str(key).lower(): value for key, value in raw_row.items()}
        if not row.get("parent_table") or not row.get("parent_column"):
            continue
        key = (
            str(row["constraint_name"]),
            str(row["child_schema"]),
            str(row["child_table"]),
            str(row["parent_schema"]),
            str(row["parent_table"]),
            str(row.get("constraint_schema") or ""),
        )
        grouped.setdefault(key, []).append(row)
    refs: list[ForeignKeyRef] = []
    for (constraint_name, child_schema, child_table, parent_schema, parent_table, _), items in grouped.items():
        ordered = sorted(items, key=lambda item: int(item.get("ordinal_position") or 0))
        refs.append(
            ForeignKeyRef(
                constraint_name=constraint_name,
                child_schema=child_schema,
                child_table=child_table,
                child_columns=[str(item["child_column"]) for item in ordered],
                parent_schema=parent_schema,
                parent_table=parent_table,
                parent_columns=[str(item["parent_column"]) for item in ordered],
            )
        )
    return refs


def is_string_type(data_type: str) -> bool:
    text = data_type.upper()
    return any(part in text for part in ["CHAR", "TEXT", "STRING", "VARCHAR", "CLOB"])


def is_numeric_type(data_type: str) -> bool:
    text = data_type.upper()
    return any(part in text for part in ["INT", "REAL", "DOUBLE", "FLOAT", "DECIMAL", "NUMERIC", "NUMBER"])


def is_date_type(data_type: str) -> bool:
    text = data_type.upper()
    return any(part in text for part in ["DATE", "TIME"])


def sentinel_label(value: Any) -> str:
    if value is None:
        return "(NULL)"
    if isinstance(value, str) and value.strip() == "":
        return "(空串)"
    label = str(value)
    if label in {"(NULL)", "(空串)"} or label.startswith("\\"):
        return "\\" + label
    return label


def mask_value(value: Any) -> str:
    if value is None:
        return "(NULL)"
    text = str(value)
    if len(text) <= 1:
        return "*"
    if re.fullmatch(r"\d{17}[\dXx]", text):
        return text[:6] + "********" + text[-4:]
    if re.fullmatch(r"1\d{10}", text):
        return text[:3] + "****" + text[-4:]
    if len(text) <= 3:
        return text[0] + "*" * (len(text) - 1)
    return text[0] + "*" * max(1, len(text) - 2) + text[-1]
