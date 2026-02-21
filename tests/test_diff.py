"""Tests for the diff engine (alter.diff)."""

from __future__ import annotations

import copy

import pytest

from alter.diff import SchemaChange, diff_schemas
from alter.schema import AlterSchema, Column, Relation, Table


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _schema(*tables: Table, relations: list[Relation] | None = None) -> AlterSchema:
    return AlterSchema(tables=list(tables), relations=relations or [])


def _table(name: str, *extra_cols: Column) -> Table:
    pk = Column(name="id", type="uuid", primary_key=True, nullable=False)
    return Table(name=name, columns=[pk, *extra_cols])


def _col(name: str, type: str = "string", **kw) -> Column:
    return Column(name=name, type=type, **kw)


def _rel(from_table: str, from_col: str, to_table: str, to_col: str = "id") -> Relation:
    return Relation(
        name=f"{from_table}_{from_col}_fk",
        from_table=from_table,
        from_column=from_col,
        to_table=to_table,
        to_column=to_col,
    )


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


def test_empty_diff_identical_schemas():
    """Identical schemas produce zero changes."""
    schema = _schema(_table("users"))
    assert diff_schemas(schema, schema) == []


def test_empty_diff_deep_copy():
    """Deep-copied identical schema also produces zero changes."""
    schema = _schema(_table("users"))
    assert diff_schemas(schema, copy.deepcopy(schema)) == []


def test_add_table():
    old = _schema()
    new = _schema(_table("users"))
    changes = diff_schemas(old, new)

    add_events = [c for c in changes if c.type == "add_table"]
    assert len(add_events) == 1
    assert add_events[0].table == "users"
    assert add_events[0].destructive is False


def test_drop_table():
    old = _schema(_table("users"))
    new = _schema()
    changes = diff_schemas(old, new)

    drop_events = [c for c in changes if c.type == "drop_table"]
    assert len(drop_events) == 1
    assert drop_events[0].table == "users"


def test_drop_table_is_destructive():
    old = _schema(_table("orders"))
    new = _schema()
    changes = diff_schemas(old, new)

    drop = next(c for c in changes if c.type == "drop_table")
    assert drop.destructive is True


def test_add_column():
    old = _schema(_table("users"))
    new = _schema(_table("users", _col("bio", "text")))
    changes = diff_schemas(old, new)

    adds = [c for c in changes if c.type == "add_column"]
    assert any(c.table == "users" and c.column == "bio" for c in adds)
    assert all(c.destructive is False for c in adds)


def test_drop_column():
    old = _schema(_table("users", _col("bio", "text")))
    new = _schema(_table("users"))
    changes = diff_schemas(old, new)

    drops = [c for c in changes if c.type == "drop_column"]
    assert any(c.table == "users" and c.column == "bio" for c in drops)
    assert all(c.destructive is True for c in drops)


def test_modify_column_type():
    old = _schema(_table("items", _col("value", "string")))
    new = _schema(_table("items", _col("value", "text")))
    changes = diff_schemas(old, new)

    mods = [c for c in changes if c.type == "modify_column"]
    assert len(mods) == 1
    assert mods[0].table == "items"
    assert mods[0].column == "value"
    assert "type" in mods[0].details
    assert mods[0].destructive is True  # type change may lose data


def test_modify_column_nullable():
    old = _schema(_table("users", _col("name", nullable=True)))
    new = _schema(_table("users", _col("name", nullable=False)))
    changes = diff_schemas(old, new)

    mods = [c for c in changes if c.type == "modify_column"]
    assert len(mods) == 1
    assert "nullable" in mods[0].details
    assert mods[0].destructive is False  # nullable change is not destructive by itself


def test_modify_column_default():
    old = _schema(_table("settings", _col("theme", default=None)))
    new = _schema(_table("settings", _col("theme", default="dark")))
    changes = diff_schemas(old, new)

    mods = [c for c in changes if c.type == "modify_column"]
    assert any("default" in c.details for c in mods)


def test_add_relation():
    users = _table("users")
    posts = _table("posts", _col("user_id", "uuid"))
    rel = _rel("posts", "user_id", "users")

    old = _schema(users, posts, relations=[])
    new = _schema(users, posts, relations=[rel])
    changes = diff_schemas(old, new)

    adds = [c for c in changes if c.type == "add_relation"]
    assert len(adds) == 1
    assert adds[0].table == "posts"
    assert adds[0].column == "user_id"
    assert adds[0].destructive is False


def test_drop_relation():
    users = _table("users")
    posts = _table("posts", _col("user_id", "uuid"))
    rel = _rel("posts", "user_id", "users")

    old = _schema(users, posts, relations=[rel])
    new = _schema(users, posts, relations=[])
    changes = diff_schemas(old, new)

    drops = [c for c in changes if c.type == "drop_relation"]
    assert len(drops) == 1
    assert drops[0].destructive is True


def test_add_index():
    old = _schema(_table("users", _col("email", index=False)))
    new = _schema(_table("users", _col("email", index=True)))
    changes = diff_schemas(old, new)

    index_adds = [c for c in changes if c.type == "add_index"]
    assert any(c.table == "users" and c.column == "email" for c in index_adds)


def test_drop_index():
    old = _schema(_table("users", _col("email", index=True)))
    new = _schema(_table("users", _col("email", index=False)))
    changes = diff_schemas(old, new)

    index_drops = [c for c in changes if c.type == "drop_index"]
    assert any(c.table == "users" and c.column == "email" for c in index_drops)


def test_multiple_changes_detected():
    """Add one table, drop another, modify a column — all detected in one diff."""
    t_users = _table("users", _col("name"))
    t_posts = _table("posts")

    old = _schema(t_users, t_posts)
    new = _schema(
        _table("users", _col("name", nullable=False)),  # nullable changed
        _table("comments"),  # new table
        # posts dropped
    )
    changes = diff_schemas(old, new)

    types = {c.type for c in changes}
    assert "add_table" in types
    assert "drop_table" in types
    assert "modify_column" in types


def test_pk_column_index_not_emitted_as_add_index():
    """Index events are not generated for PK columns (they're indexed implicitly)."""
    pk_col = Column(name="id", type="uuid", primary_key=True, nullable=False, index=True)
    old = _schema(Table(name="t", columns=[]))
    new = _schema(Table(name="t", columns=[pk_col]))
    changes = diff_schemas(old, new)

    # add_table should appear, but NOT add_index for the PK column
    index_adds = [c for c in changes if c.type == "add_index"]
    assert not any(c.column == "id" for c in index_adds)
