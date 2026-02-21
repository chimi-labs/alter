"""Tests for schema validation (alter.validate)."""

from __future__ import annotations

import pytest

from alter.schema import AlterSchema, Column, Relation, Table
from alter.validate import ValidationIssue, validate_schema


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
