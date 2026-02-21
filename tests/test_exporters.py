"""Tests for SQL DDL and Mermaid exporters."""

from __future__ import annotations

import pytest

from alter.exporters.mermaid import export_mermaid
from alter.exporters.sql import export_sql
from alter.importers.sql import import_sql
from alter.schema import AlterSchema, Column, Relation, Table


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _simple_schema() -> AlterSchema:
    return AlterSchema(
        tables=[
            Table(
                name="users",
                columns=[
                    Column(name="id", type="uuid", primary_key=True, nullable=False),
                    Column(name="email", type="string", max_length=255, unique=True, nullable=False),
                    Column(name="name", type="text"),
                    Column(name="score", type="int", nullable=True, default="0"),
                ],
            )
        ]
    )


def _schema_with_fk() -> AlterSchema:
    return AlterSchema(
        tables=[
            Table(
                name="users",
                columns=[
                    Column(name="id", type="uuid", primary_key=True, nullable=False),
                ],
            ),
            Table(
                name="posts",
                columns=[
                    Column(name="id", type="uuid", primary_key=True, nullable=False),
                    Column(name="user_id", type="uuid", nullable=False),
                    Column(name="title", type="string", max_length=200, nullable=False),
                ],
            ),
        ],
        relations=[
            Relation(
                name="posts_user_fk",
                from_table="posts",
                from_column="user_id",
                to_table="users",
                to_column="id",
                on_delete="CASCADE",
            )
        ],
    )


# ---------------------------------------------------------------------------
# SQL DDL exporter
# ---------------------------------------------------------------------------


class TestExportSql:
    def test_returns_string(self):
        sql = export_sql(_simple_schema())
        assert isinstance(sql, str)

    def test_contains_create_table(self):
        sql = export_sql(_simple_schema())
        assert "CREATE TABLE users" in sql

    def test_primary_key_present(self):
        sql = export_sql(_simple_schema())
        assert "PRIMARY KEY" in sql
        assert "id" in sql

    def test_not_null_emitted(self):
        sql = export_sql(_simple_schema())
        assert "NOT NULL" in sql

    def test_unique_emitted(self):
        sql = export_sql(_simple_schema())
        assert "UNIQUE" in sql

    def test_varchar_with_max_length(self):
        sql = export_sql(_simple_schema())
        assert "VARCHAR(255)" in sql

    def test_default_value_emitted(self):
        sql = export_sql(_simple_schema())
        assert "DEFAULT" in sql

    def test_foreign_key_constraint_emitted(self):
        sql = export_sql(_schema_with_fk())
        assert "FOREIGN KEY" in sql
        assert "REFERENCES users" in sql

    def test_on_delete_emitted(self):
        sql = export_sql(_schema_with_fk())
        assert "ON DELETE CASCADE" in sql

    def test_multiple_tables(self):
        sql = export_sql(_schema_with_fk())
        assert "CREATE TABLE users" in sql
        assert "CREATE TABLE posts" in sql

    def test_empty_schema_returns_empty(self):
        sql = export_sql(AlterSchema())
        assert sql == ""

    def test_uuid_default(self):
        schema = AlterSchema(
            tables=[
                Table(
                    name="items",
                    columns=[
                        Column(name="id", type="uuid", primary_key=True, nullable=False, default="uuid4"),
                    ],
                )
            ]
        )
        sql = export_sql(schema)
        assert "gen_random_uuid()" in sql

    def test_utcnow_default(self):
        schema = AlterSchema(
            tables=[
                Table(
                    name="events",
                    columns=[
                        Column(name="id", type="uuid", primary_key=True, nullable=False),
                        Column(name="created_at", type="datetime", default="utcnow"),
                    ],
                )
            ]
        )
        sql = export_sql(schema)
        assert "now()" in sql

    def test_string_default_quoted(self):
        schema = AlterSchema(
            tables=[
                Table(
                    name="settings",
                    columns=[
                        Column(name="id", type="uuid", primary_key=True, nullable=False),
                        Column(name="theme", type="string", max_length=50, default="dark"),
                    ],
                )
            ]
        )
        sql = export_sql(schema)
        assert "'dark'" in sql


# ---------------------------------------------------------------------------
# Mermaid exporter
# ---------------------------------------------------------------------------


class TestExportMermaid:
    def test_starts_with_erdiagram(self):
        mermaid = export_mermaid(_simple_schema())
        assert mermaid.startswith("erDiagram")

    def test_table_entity_present(self):
        mermaid = export_mermaid(_simple_schema())
        assert "users {" in mermaid

    def test_pk_annotation(self):
        mermaid = export_mermaid(_simple_schema())
        assert "PK" in mermaid

    def test_uk_annotation(self):
        mermaid = export_mermaid(_simple_schema())
        assert "UK" in mermaid

    def test_fk_annotation(self):
        schema = AlterSchema(
            tables=[
                Table(
                    name="posts",
                    columns=[
                        Column(name="id", type="uuid", primary_key=True, nullable=False),
                        Column(name="user_id", type="uuid", foreign_key="users.id"),
                    ],
                )
            ]
        )
        mermaid = export_mermaid(schema)
        assert "FK" in mermaid

    def test_relation_line_emitted(self):
        mermaid = export_mermaid(_schema_with_fk())
        assert "posts" in mermaid
        assert "users" in mermaid
        # Should have a relation line between them
        assert "||" in mermaid or "}o" in mermaid

    def test_empty_schema(self):
        mermaid = export_mermaid(AlterSchema())
        assert mermaid.startswith("erDiagram")

    def test_ends_with_newline(self):
        mermaid = export_mermaid(_simple_schema())
        assert mermaid.endswith("\n")


# ---------------------------------------------------------------------------
# Round-trip: schema → SQL → import → check structure
# ---------------------------------------------------------------------------


class TestSqlRoundTrip:
    def test_table_names_preserved(self):
        """Exported SQL, when re-imported, yields the same table names."""
        original = _schema_with_fk()
        sql = export_sql(original)
        reimported = import_sql(sql)

        orig_names = {t.name for t in original.tables}
        new_names = {t.name for t in reimported.tables}
        assert orig_names == new_names

    def test_column_names_preserved(self):
        original = _simple_schema()
        sql = export_sql(original)
        reimported = import_sql(sql)

        orig_cols = {c.name for c in original.tables[0].columns}
        new_cols = {c.name for c in reimported.tables[0].columns}
        assert orig_cols == new_cols

    def test_primary_key_preserved(self):
        original = _simple_schema()
        sql = export_sql(original)
        reimported = import_sql(sql)

        pk_cols = [c for c in reimported.tables[0].columns if c.primary_key]
        assert len(pk_cols) == 1
        assert pk_cols[0].name == "id"

    def test_fk_relation_preserved(self):
        """FK relations are reconstructed from FOREIGN KEY clauses in the SQL."""
        original = _schema_with_fk()
        sql = export_sql(original)
        reimported = import_sql(sql)

        assert len(reimported.relations) >= 1
        rel = next(
            (r for r in reimported.relations if r.from_table == "posts"), None
        )
        assert rel is not None
        assert rel.to_table == "users"

    def test_unique_constraint_preserved(self):
        original = _simple_schema()
        sql = export_sql(original)
        reimported = import_sql(sql)

        email = next(
            (c for c in reimported.tables[0].columns if c.name == "email"), None
        )
        assert email is not None
        assert email.unique is True

    def test_not_null_preserved(self):
        original = _simple_schema()
        sql = export_sql(original)
        reimported = import_sql(sql)

        email = next(
            (c for c in reimported.tables[0].columns if c.name == "email"), None
        )
        assert email is not None
        assert email.nullable is False
