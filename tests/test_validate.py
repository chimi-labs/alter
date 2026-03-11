"""Tests for schema validation (alter.validate)."""

from __future__ import annotations

import pytest

from alter.schema import AlterSchema, Column, Relation, Table
from alter.validate import ValidationIssue, _parse_fk_reference, validate_schema


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_table(name: str = "users") -> Table:
    return Table(
        name=name,
        columns=[
            Column(name="id", type="uuid", primary_key=True, nullable=False),
            Column(name="email", type="string"),
        ],
    )


def _col(**kw) -> Column:
    defaults = dict(name="col", type="string")
    defaults.update(kw)
    return Column(**defaults)


def _issues(schema: AlterSchema, severity: str | None = None) -> list[ValidationIssue]:
    issues = validate_schema(schema)
    if severity:
        return [i for i in issues if i.severity == severity]
    return issues


# ---------------------------------------------------------------------------
# Valid schema — no errors
# ---------------------------------------------------------------------------


def test_valid_schema_no_errors():
    schema = AlterSchema(tables=[_valid_table()])
    errors = _issues(schema, "error")
    assert errors == []


def test_valid_schema_with_fk_no_errors():
    users = _valid_table("users")
    posts = Table(
        name="posts",
        columns=[
            Column(name="id", type="uuid", primary_key=True, nullable=False),
            Column(name="user_id", type="uuid", foreign_key="users.id", index=True),
        ],
    )
    schema = AlterSchema(tables=[users, posts])
    errors = _issues(schema, "error")
    assert errors == []


# ---------------------------------------------------------------------------
# Primary key checks
# ---------------------------------------------------------------------------


def test_missing_pk_produces_warning():
    table = Table(name="logs", columns=[Column(name="message", type="text")])
    schema = AlterSchema(tables=[table])
    warnings = _issues(schema, "warning")
    assert any("primary key" in i.message.lower() for i in warnings)


def test_table_with_pk_no_pk_warning():
    schema = AlterSchema(tables=[_valid_table()])
    warnings = _issues(schema, "warning")
    pk_warnings = [w for w in warnings if "primary key" in w.message.lower()]
    assert pk_warnings == []


# ---------------------------------------------------------------------------
# Duplicate column names
# ---------------------------------------------------------------------------


def test_duplicate_column_name_is_error():
    table = Table(
        name="users",
        columns=[
            Column(name="id", type="uuid", primary_key=True, nullable=False),
            Column(name="id", type="string"),  # duplicate!
        ],
    )
    schema = AlterSchema(tables=[table])
    errors = _issues(schema, "error")
    assert any("duplicate" in i.message.lower() for i in errors)


# ---------------------------------------------------------------------------
# Dangling foreign key references
# ---------------------------------------------------------------------------


def test_dangling_fk_unknown_table_is_error():
    posts = Table(
        name="posts",
        columns=[
            Column(name="id", type="uuid", primary_key=True, nullable=False),
            Column(name="user_id", type="uuid", foreign_key="nonexistent_table.id"),
        ],
    )
    schema = AlterSchema(tables=[posts])
    errors = _issues(schema, "error")
    assert any("unknown table" in i.message.lower() for i in errors)


def test_dangling_fk_unknown_column_is_error():
    users = _valid_table("users")
    posts = Table(
        name="posts",
        columns=[
            Column(name="id", type="uuid", primary_key=True, nullable=False),
            Column(name="user_id", type="uuid", foreign_key="users.no_such_col"),
        ],
    )
    schema = AlterSchema(tables=[users, posts])
    errors = _issues(schema, "error")
    assert any("unknown column" in i.message.lower() for i in errors)


def test_valid_fk_no_dangling_error():
    users = _valid_table("users")
    posts = Table(
        name="posts",
        columns=[
            Column(name="id", type="uuid", primary_key=True, nullable=False),
            Column(name="user_id", type="uuid", foreign_key="users.id", index=True),
        ],
    )
    schema = AlterSchema(tables=[users, posts])
    errors = _issues(schema, "error")
    assert not any("unknown" in i.message.lower() for i in errors)


def test_invalid_fk_format_is_error():
    posts = Table(
        name="posts",
        columns=[
            Column(name="id", type="uuid", primary_key=True, nullable=False),
            Column(name="user_id", type="uuid", foreign_key="bad_format_no_dot"),
        ],
    )
    schema = AlterSchema(tables=[posts])
    errors = _issues(schema, "error")
    assert any("format" in i.message.lower() for i in errors)


# ---------------------------------------------------------------------------
# FK index suggestions
# ---------------------------------------------------------------------------


def test_fk_without_index_suggests_info():
    users = _valid_table("users")
    posts = Table(
        name="posts",
        columns=[
            Column(name="id", type="uuid", primary_key=True, nullable=False),
            Column(name="user_id", type="uuid", foreign_key="users.id", index=False),
        ],
    )
    schema = AlterSchema(tables=[users, posts])
    infos = _issues(schema, "info")
    assert any("index" in i.message.lower() for i in infos)


def test_fk_with_index_no_info_suggestion():
    users = _valid_table("users")
    posts = Table(
        name="posts",
        columns=[
            Column(name="id", type="uuid", primary_key=True, nullable=False),
            Column(name="user_id", type="uuid", foreign_key="users.id", index=True),
        ],
    )
    schema = AlterSchema(tables=[users, posts])
    infos = _issues(schema, "info")
    fk_index_infos = [i for i in infos if "FK" in i.message or "index" in i.message.lower()]
    # Should have no index suggestion for this column
    col_infos = [i for i in fk_index_infos if i.column == "user_id"]
    assert col_infos == []


# ---------------------------------------------------------------------------
# Empty names
# ---------------------------------------------------------------------------


def test_empty_table_name_is_error():
    table = Table(
        name="",
        columns=[Column(name="id", type="uuid", primary_key=True, nullable=False)],
    )
    schema = AlterSchema(tables=[table])
    errors = _issues(schema, "error")
    assert any("empty" in i.message.lower() or "name" in i.message.lower() for i in errors)


# ---------------------------------------------------------------------------
# Relation validation
# ---------------------------------------------------------------------------


def test_relation_unknown_from_table_is_error():
    users = _valid_table("users")
    rel = Relation(
        name="bad_rel",
        from_table="nonexistent",
        from_column="user_id",
        to_table="users",
        to_column="id",
    )
    schema = AlterSchema(tables=[users], relations=[rel])
    errors = _issues(schema, "error")
    assert any("from_table" in i.message.lower() or "nonexistent" in i.message for i in errors)


def test_relation_unknown_to_table_is_error():
    users = _valid_table("users")
    rel = Relation(
        name="bad_rel",
        from_table="users",
        from_column="id",
        to_table="nonexistent",
        to_column="id",
    )
    schema = AlterSchema(tables=[users], relations=[rel])
    errors = _issues(schema, "error")
    assert any("to_table" in i.message.lower() or "nonexistent" in i.message for i in errors)


# ---------------------------------------------------------------------------
# Schema-qualified foreign keys  (fix: validator must accept 'schema.table.col')
# ---------------------------------------------------------------------------


class TestParseFkReference:
    """Unit tests for the _parse_fk_reference helper."""

    def test_two_part_unqualified(self):
        schema, table, col = _parse_fk_reference("users.id")
        assert schema is None
        assert table == "users"
        assert col == "id"

    def test_three_part_schema_qualified(self):
        schema, table, col = _parse_fk_reference("myschema.orders.id")
        assert schema == "myschema"
        assert table == "orders"
        assert col == "id"

    def test_one_part_returns_empty_strings(self):
        schema, table, col = _parse_fk_reference("no_dot")
        assert table == ""
        assert col == ""

    def test_four_part_returns_empty_strings(self):
        schema, table, col = _parse_fk_reference("a.b.c.d")
        assert table == ""
        assert col == ""


class TestSchemaQualifiedForeignKeys:
    """Validator must not reject schema-qualified FK strings."""

    def _schema_qualified_fk_schema(self) -> AlterSchema:
        orders = Table(
            name="orders",
            schema_name="myschema",
            columns=[
                Column(name="id", type="uuid", primary_key=True, nullable=False),
            ],
        )
        order_items = Table(
            name="order_items",
            schema_name="myschema",
            columns=[
                Column(name="id", type="uuid", primary_key=True, nullable=False),
                Column(
                    name="order_id",
                    type="uuid",
                    foreign_key="myschema.orders.id",
                    index=True,
                ),
            ],
        )
        return AlterSchema(tables=[orders, order_items])

    def test_schema_qualified_fk_no_format_error(self):
        """'schema.table.column' must not produce a format error."""
        errors = _issues(self._schema_qualified_fk_schema(), "error")
        format_errors = [e for e in errors if "format" in e.message.lower()]
        assert format_errors == []

    def test_schema_qualified_fk_resolved_table_found(self):
        """Referenced table is found by bare name despite schema prefix in FK string."""
        errors = _issues(self._schema_qualified_fk_schema(), "error")
        unknown_errors = [e for e in errors if "unknown table" in e.message.lower()]
        assert unknown_errors == []

    def test_schema_qualified_fk_resolved_column_found(self):
        """Referenced column is found correctly when FK is schema-qualified."""
        errors = _issues(self._schema_qualified_fk_schema(), "error")
        col_errors = [e for e in errors if "unknown column" in e.message.lower()]
        assert col_errors == []

    def test_schema_qualified_fk_zero_errors(self):
        """A valid schema-qualified FK produces zero validation errors."""
        errors = _issues(self._schema_qualified_fk_schema(), "error")
        assert errors == []

    def test_schema_qualified_fk_dangling_table_still_errors(self):
        """A schema-qualified FK to a nonexistent table still raises an error."""
        table = Table(
            name="items",
            columns=[
                Column(name="id", type="uuid", primary_key=True, nullable=False),
                Column(
                    name="order_id",
                    type="uuid",
                    foreign_key="myschema.nonexistent_table.id",
                ),
            ],
        )
        errors = _issues(AlterSchema(tables=[table]), "error")
        assert any("unknown table" in e.message.lower() for e in errors)

    def test_unqualified_fk_still_valid(self):
        """Existing plain 'table.column' FKs must still pass without errors."""
        users = _valid_table("users")
        posts = Table(
            name="posts",
            columns=[
                Column(name="id", type="uuid", primary_key=True, nullable=False),
                Column(name="user_id", type="uuid", foreign_key="users.id", index=True),
            ],
        )
        errors = _issues(AlterSchema(tables=[users, posts]), "error")
        assert errors == []

    def test_error_message_mentions_both_formats(self):
        """Format-error message must describe both valid forms."""
        table = Table(
            name="items",
            columns=[
                Column(name="id", type="uuid", primary_key=True, nullable=False),
                Column(name="x", type="string", foreign_key="no_dot_at_all"),
            ],
        )
        errors = _issues(AlterSchema(tables=[table]), "error")
        format_errors = [e for e in errors if "format" in e.message.lower()]
        assert format_errors
        assert "schema.table.column" in format_errors[0].message
