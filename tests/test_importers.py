"""Tests for SQL DDL and .alter file importers."""

from __future__ import annotations

from pathlib import Path

import pytest

from alter.importers.alter_file import import_alter_file
from alter.importers.sql import import_sql
from alter.schema import AlterSchema, Column, Position, Table


# ---------------------------------------------------------------------------
# SQL DDL importer
# ---------------------------------------------------------------------------

_SINGLE_TABLE_SQL = """
CREATE TABLE users (
    id UUID PRIMARY KEY,
    email VARCHAR(255) NOT NULL UNIQUE,
    name TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);
"""

_MULTI_TABLE_SQL = """
CREATE TABLE users (
    id UUID PRIMARY KEY,
    email VARCHAR(255) NOT NULL UNIQUE
);

CREATE TABLE posts (
    id UUID PRIMARY KEY,
    title VARCHAR(200) NOT NULL,
    user_id UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE
);

CREATE TABLE comments (
    id UUID PRIMARY KEY,
    body TEXT NOT NULL,
    post_id UUID NOT NULL REFERENCES posts (id) ON DELETE CASCADE
);
"""

_TABLE_LEVEL_FK_SQL = """
CREATE TABLE organizations (
    id UUID PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE memberships (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    user_id UUID NOT NULL,
    FOREIGN KEY (org_id) REFERENCES organizations (id) ON DELETE CASCADE
);
"""

_DEFAULT_VALS_SQL = """
CREATE TABLE settings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    theme VARCHAR(50) DEFAULT 'light',
    is_active BOOLEAN DEFAULT TRUE,
    score INTEGER DEFAULT 0
);
"""


class TestImportSqlSingleTable:
    def test_returns_alter_schema(self):
        schema = import_sql(_SINGLE_TABLE_SQL)
        assert isinstance(schema, AlterSchema)

    def test_table_name_extracted(self):
        schema = import_sql(_SINGLE_TABLE_SQL)
        assert len(schema.tables) == 1
        assert schema.tables[0].name == "users"

    def test_column_count(self):
        schema = import_sql(_SINGLE_TABLE_SQL)
        cols = {c.name for c in schema.tables[0].columns}
        assert "id" in cols
        assert "email" in cols
        assert "name" in cols
        assert "created_at" in cols

    def test_pk_column_detected(self):
        schema = import_sql(_SINGLE_TABLE_SQL)
        pk_cols = [c for c in schema.tables[0].columns if c.primary_key]
        assert len(pk_cols) == 1
        assert pk_cols[0].name == "id"

    def test_not_null_detected(self):
        schema = import_sql(_SINGLE_TABLE_SQL)
        email = next(c for c in schema.tables[0].columns if c.name == "email")
        assert email.nullable is False

    def test_unique_detected(self):
        schema = import_sql(_SINGLE_TABLE_SQL)
        email = next(c for c in schema.tables[0].columns if c.name == "email")
        assert email.unique is True

    def test_type_mapping(self):
        schema = import_sql(_SINGLE_TABLE_SQL)
        col_map = {c.name: c for c in schema.tables[0].columns}
        assert col_map["id"].type == "uuid"
        assert col_map["email"].type == "string"
        assert col_map["name"].type == "text"
        assert col_map["created_at"].type == "datetime"

    def test_default_normalised(self):
        schema = import_sql(_SINGLE_TABLE_SQL)
        ts_col = next(c for c in schema.tables[0].columns if c.name == "created_at")
        assert ts_col.default == "utcnow"

    def test_orm_passed_through(self):
        schema = import_sql(_SINGLE_TABLE_SQL, orm="sqlalchemy")
        assert schema.orm == "sqlalchemy"


class TestImportSqlMultiTable:
    def test_three_tables_imported(self):
        schema = import_sql(_MULTI_TABLE_SQL)
        assert len(schema.tables) == 3

    def test_table_names(self):
        schema = import_sql(_MULTI_TABLE_SQL)
        names = {t.name for t in schema.tables}
        assert names == {"users", "posts", "comments"}

    def test_inline_fk_creates_relation(self):
        schema = import_sql(_MULTI_TABLE_SQL)
        assert len(schema.relations) >= 1
        rel_tables = {(r.from_table, r.to_table) for r in schema.relations}
        assert ("posts", "users") in rel_tables

    def test_on_delete_cascade_captured(self):
        schema = import_sql(_MULTI_TABLE_SQL)
        posts_fk = next(
            (r for r in schema.relations if r.from_table == "posts"), None
        )
        assert posts_fk is not None
        assert posts_fk.on_delete == "CASCADE"

    def test_grid_positions_assigned(self):
        schema = import_sql(_MULTI_TABLE_SQL)
        for table in schema.tables:
            assert isinstance(table.position, Position)
            # x, y should be non-negative integers
            assert table.position.x >= 0
            assert table.position.y >= 0

    def test_no_duplicate_positions(self):
        schema = import_sql(_MULTI_TABLE_SQL)
        positions = [(t.position.x, t.position.y) for t in schema.tables]
        assert len(positions) == len(set(positions)), "Tables should not overlap"


class TestImportSqlTableLevelFK:
    def test_table_level_fk_creates_relation(self):
        schema = import_sql(_TABLE_LEVEL_FK_SQL)
        assert len(schema.relations) >= 1
        rel = next(
            (r for r in schema.relations if r.from_table == "memberships"), None
        )
        assert rel is not None
        assert rel.to_table == "organizations"


class TestImportSqlDefaults:
    def test_uuid_default_normalised(self):
        schema = import_sql(_DEFAULT_VALS_SQL)
        id_col = next(c for c in schema.tables[0].columns if c.name == "id")
        assert id_col.default == "uuid4"

    def test_string_default_extracted(self):
        schema = import_sql(_DEFAULT_VALS_SQL)
        theme = next(c for c in schema.tables[0].columns if c.name == "theme")
        assert theme.default == "light"

    def test_numeric_default_extracted(self):
        schema = import_sql(_DEFAULT_VALS_SQL)
        score = next(c for c in schema.tables[0].columns if c.name == "score")
        assert score.default == "0"


class TestImportSqlIfNotExists:
    def test_if_not_exists_handled(self):
        sql = "CREATE TABLE IF NOT EXISTS widgets (id UUID PRIMARY KEY);"
        schema = import_sql(sql)
        assert len(schema.tables) == 1
        assert schema.tables[0].name == "widgets"


class TestImportSqlSchemaPrefix:
    def test_schema_prefix_stripped(self):
        sql = "CREATE TABLE public.products (id UUID PRIMARY KEY);"
        schema = import_sql(sql)
        assert len(schema.tables) == 1
        assert schema.tables[0].name == "products"


# ---------------------------------------------------------------------------
# .alter file importer
# ---------------------------------------------------------------------------


class TestImportAlterFile:
    def test_loads_tables(self, tmp_path: Path):
        source = AlterSchema(
            tables=[
                Table(
                    name="items",
                    columns=[Column(name="id", type="uuid", primary_key=True, nullable=False)],
                    position=Position(x=150, y=300),
                )
            ]
        )
        path = tmp_path / "source.alter"
        source.save(path)

        loaded = import_alter_file(path)
        assert len(loaded.tables) == 1
        assert loaded.tables[0].name == "items"

    def test_preserves_positions(self, tmp_path: Path):
        source = AlterSchema(
            tables=[
                Table(
                    name="items",
                    columns=[Column(name="id", type="uuid", primary_key=True, nullable=False)],
                    position=Position(x=150, y=300),
                )
            ]
        )
        path = tmp_path / "source.alter"
        source.save(path)

        loaded = import_alter_file(path)
        assert loaded.tables[0].position.x == 150
        assert loaded.tables[0].position.y == 300

    def test_preserves_relations(self, tmp_path: Path):
        from alter.schema import Relation

        source = AlterSchema(
            tables=[
                Table(
                    name="users",
                    columns=[Column(name="id", type="uuid", primary_key=True, nullable=False)],
                ),
                Table(
                    name="posts",
                    columns=[
                        Column(name="id", type="uuid", primary_key=True, nullable=False),
                        Column(name="user_id", type="uuid"),
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
                )
            ],
        )
        path = tmp_path / "source.alter"
        source.save(path)

        loaded = import_alter_file(path)
        assert len(loaded.relations) == 1
        assert loaded.relations[0].from_table == "posts"
