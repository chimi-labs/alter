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

    # --- _col_attr: FK+UK fix (elif → if) ---

    def test_fk_and_unique_column_shows_both_annotations(self):
        """Column that is both FK and unique must show FK,UK (one-to-one pattern)."""
        from alter.exporters.mermaid import _col_attr
        col = Column(
            name="user_id", type="uuid",
            foreign_key="users.id", unique=True,
            nullable=False,
        )
        attr = _col_attr(col)
        assert "FK" in attr
        assert "UK" in attr

    def test_fk_only_column_shows_fk_not_uk(self):
        from alter.exporters.mermaid import _col_attr
        col = Column(name="user_id", type="uuid", foreign_key="users.id", unique=False)
        attr = _col_attr(col)
        assert "FK" in attr
        assert "UK" not in attr

    def test_unique_only_column_shows_uk_not_fk(self):
        from alter.exporters.mermaid import _col_attr
        col = Column(name="email", type="string", unique=True)
        attr = _col_attr(col)
        assert "UK" in attr
        assert "FK" not in attr

    def test_pk_column_never_gets_uk_even_when_unique(self):
        """Primary keys are implicitly unique; adding UK would be redundant."""
        from alter.exporters.mermaid import _col_attr
        col = Column(name="id", type="uuid", primary_key=True, unique=True, nullable=False)
        attr = _col_attr(col)
        assert "PK" in attr
        assert "UK" not in attr

    def test_full_export_one_to_one_relationship(self):
        """Full export_mermaid with a FK+unique column must include FK,UK in output."""
        schema = AlterSchema(
            tables=[
                Table(
                    name="users",
                    columns=[
                        Column(name="id", type="uuid", primary_key=True, nullable=False),
                    ],
                ),
                Table(
                    name="profiles",
                    columns=[
                        Column(name="id", type="uuid", primary_key=True, nullable=False),
                        Column(
                            name="user_id", type="uuid",
                            foreign_key="users.id", unique=True, nullable=False,
                        ),
                    ],
                ),
            ]
        )
        mermaid = export_mermaid(schema)
        assert "FK" in mermaid
        assert "UK" in mermaid
        # Both annotations must appear together on the user_id line
        for line in mermaid.splitlines():
            if "user_id" in line:
                assert "FK" in line
                assert "UK" in line
                break
        else:
            pytest.fail("user_id column line not found in mermaid output")


# ---------------------------------------------------------------------------
# Schema-qualified names — SQL exporter
# ---------------------------------------------------------------------------


def _schema_with_schema_name() -> AlterSchema:
    """Single table with schema_name set."""
    return AlterSchema(
        tables=[
            Table(
                name="orders",
                schema_name="sales",
                columns=[
                    Column(name="id", type="uuid", primary_key=True, nullable=False),
                    Column(name="total", type="float", nullable=False),
                ],
            )
        ]
    )


def _cross_schema_fk() -> AlterSchema:
    """Two tables in the same schema; FK must reference qualified name."""
    return AlterSchema(
        tables=[
            Table(
                name="customers",
                schema_name="sales",
                columns=[
                    Column(name="id", type="uuid", primary_key=True, nullable=False),
                ],
            ),
            Table(
                name="orders",
                schema_name="sales",
                columns=[
                    Column(name="id", type="uuid", primary_key=True, nullable=False),
                    Column(name="customer_id", type="uuid", nullable=False),
                ],
            ),
        ],
        relations=[
            Relation(
                name="orders_customer_fk",
                from_table="orders",
                from_column="customer_id",
                to_table="customers",
                to_column="id",
            )
        ],
    )


class TestExportSqlSchemaQualified:
    def test_create_table_uses_qualified_name(self):
        sql = export_sql(_schema_with_schema_name())
        assert "CREATE TABLE sales.orders" in sql

    def test_no_schema_unaffected(self):
        """Tables without schema_name keep plain unqualified names."""
        sql = export_sql(_simple_schema())
        assert "CREATE TABLE users" in sql
        assert "." not in sql.split("CREATE TABLE")[1].split("(")[0].strip()

    def test_fk_references_qualified_name(self):
        sql = export_sql(_cross_schema_fk())
        assert "REFERENCES sales.customers" in sql

    def test_both_tables_qualified(self):
        sql = export_sql(_cross_schema_fk())
        assert "CREATE TABLE sales.customers" in sql
        assert "CREATE TABLE sales.orders" in sql

    def test_mixed_schema_and_no_schema(self):
        """FK from schemaless table to schema table — reference is qualified."""
        schema = AlterSchema(
            tables=[
                Table(
                    name="users",
                    columns=[
                        Column(name="id", type="uuid", primary_key=True, nullable=False),
                    ],
                ),
                Table(
                    name="events",
                    schema_name="analytics",
                    columns=[
                        Column(name="id", type="uuid", primary_key=True, nullable=False),
                        Column(name="user_id", type="uuid", nullable=False),
                    ],
                ),
            ],
            relations=[
                Relation(
                    name="events_user_fk",
                    from_table="events",
                    from_column="user_id",
                    to_table="users",
                    to_column="id",
                )
            ],
        )
        sql = export_sql(schema)
        assert "CREATE TABLE analytics.events" in sql
        assert "CREATE TABLE users" in sql
        # FK references the unqualified table (no schema on users)
        assert "REFERENCES users (id)" in sql


# ---------------------------------------------------------------------------
# Schema-qualified names — Mermaid exporter
# ---------------------------------------------------------------------------


class TestExportMermaidSchemaQualified:
    def test_entity_name_uses_schema_prefix(self):
        mermaid = export_mermaid(_schema_with_schema_name())
        assert "sales_orders {" in mermaid

    def test_no_schema_entity_name_unchanged(self):
        mermaid = export_mermaid(_simple_schema())
        assert "users {" in mermaid

    def test_relation_lines_use_qualified_names(self):
        mermaid = export_mermaid(_cross_schema_fk())
        # Both ends of the relation should use the schema-prefixed names.
        assert "sales_orders" in mermaid
        assert "sales_customers" in mermaid
        # The relation line itself must reference the qualified identifiers.
        lines = mermaid.splitlines()
        rel_lines = [l for l in lines if "||" in l or "}o" in l or "o{" in l]
        assert any("sales_orders" in l and "sales_customers" in l for l in rel_lines)

    def test_entity_block_columns_intact(self):
        mermaid = export_mermaid(_schema_with_schema_name())
        assert "id" in mermaid
        assert "total" in mermaid


# ---------------------------------------------------------------------------
# Round-trip: schema → SQL → import → check structure
# ---------------------------------------------------------------------------


class TestSqlRoundTrip:
    def test_table_names_preserved(self):
        """Exported SQL, when re-imported, yields the same table names."""
        original = _schema_with_fk()
        sql = export_sql(original)
        reimported = import_sql(sql).schema

        orig_names = {t.name for t in original.tables}
        new_names = {t.name for t in reimported.tables}
        assert orig_names == new_names

    def test_column_names_preserved(self):
        original = _simple_schema()
        sql = export_sql(original)
        reimported = import_sql(sql).schema

        orig_cols = {c.name for c in original.tables[0].columns}
        new_cols = {c.name for c in reimported.tables[0].columns}
        assert orig_cols == new_cols

    def test_primary_key_preserved(self):
        original = _simple_schema()
        sql = export_sql(original)
        reimported = import_sql(sql).schema

        pk_cols = [c for c in reimported.tables[0].columns if c.primary_key]
        assert len(pk_cols) == 1
        assert pk_cols[0].name == "id"

    def test_fk_relation_preserved(self):
        """FK relations are reconstructed from FOREIGN KEY clauses in the SQL."""
        original = _schema_with_fk()
        sql = export_sql(original)
        reimported = import_sql(sql).schema

        assert len(reimported.relations) >= 1
        rel = next(
            (r for r in reimported.relations if r.from_table == "posts"), None
        )
        assert rel is not None
        assert rel.to_table == "users"

    def test_unique_constraint_preserved(self):
        original = _simple_schema()
        sql = export_sql(original)
        reimported = import_sql(sql).schema

        email = next(
            (c for c in reimported.tables[0].columns if c.name == "email"), None
        )
        assert email is not None
        assert email.unique is True

    def test_not_null_preserved(self):
        original = _simple_schema()
        sql = export_sql(original)
        reimported = import_sql(sql).schema

        email = next(
            (c for c in reimported.tables[0].columns if c.name == "email"), None
        )
        assert email is not None
        assert email.nullable is False


# ---------------------------------------------------------------------------
# _format_default — single-quote escaping
# ---------------------------------------------------------------------------


class TestFormatDefaultQuoteEscaping:
    """_format_default must produce valid SQL for string defaults with quotes."""

    def setup_method(self):
        from alter.exporters.sql import _format_default
        self._fmt = _format_default

    def test_apostrophe_in_default_is_escaped(self):
        assert self._fmt("it's") == "'it''s'"

    def test_name_with_apostrophe_is_escaped(self):
        assert self._fmt("O'Brien") == "'O''Brien'"

    def test_plain_string_unchanged(self):
        assert self._fmt("no quotes") == "'no quotes'"

    def test_all_quotes_string(self):
        # Input: three single quotes.  Each is doubled → six quotes inside,
        # then wrapped in a pair of delimiters → eight single quotes total.
        result = self._fmt("'" * 3)
        expected = "'" + "''" * 3 + "'"   # = '''''''' (8 chars)
        assert result == expected

    def test_multiple_apostrophes_all_escaped(self):
        result = self._fmt("it's a test, isn't it")
        assert result == "'it''s a test, isn''t it'"

    def test_export_sql_with_quoted_default(self):
        """Full export_sql pipeline: column default with apostrophe → valid DDL."""
        from alter.exporters.sql import export_sql
        from alter.schema import AlterSchema, Column, Table
        schema = AlterSchema(
            orm="sqlmodel",
            tables=[Table(name="greet", columns=[
                Column(name="id", type="uuid", primary_key=True, nullable=False),
                Column(name="label", type="string", nullable=True, default="it's"),
            ])],
        )
        sql = export_sql(schema)
        assert "DEFAULT 'it''s'" in sql
        assert "DEFAULT 'it's'" not in sql  # the broken form must be absent
