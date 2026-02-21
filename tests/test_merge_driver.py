"""Tests for src/alter/merge_driver.py — three-way merge of .alter files."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alter.merge_driver import MergeResult, merge_schemas, run_merge_driver
from alter.schema import AlterSchema, Column, Table


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _col(name: str, col_type: str = "string") -> Column:
    return Column(name=name, type=col_type)


def _id_col() -> Column:
    return Column(name="id", type="uuid", primary_key=True, nullable=False)


def _table(name: str, extra_cols: list[Column] | None = None) -> Table:
    cols = [_id_col()]
    if extra_cols:
        cols.extend(extra_cols)
    return Table(name=name, columns=cols)


def _schema(*table_names: str) -> AlterSchema:
    """Build a minimal AlterSchema with the named tables (each has only id col)."""
    return AlterSchema(tables=[_table(n) for n in table_names])


def _schema_with_tables(tables: list[Table]) -> AlterSchema:
    return AlterSchema(tables=tables)


# ---------------------------------------------------------------------------
# merge_schemas — no conflicts
# ---------------------------------------------------------------------------


def test_merge_identical_no_conflicts() -> None:
    """base == ours == theirs → clean merge, no conflicts."""
    base = _schema("users", "posts")
    result = merge_schemas(base, base, base)
    assert not result.has_conflicts
    assert len(result.schema.tables) == 2


def test_merge_independent_table_additions_auto_merged() -> None:
    """ours adds TableA, theirs adds TableB → both present, no conflict."""
    base = _schema("users")
    ours = _schema("users", "orders")
    theirs = _schema("users", "invoices")

    result = merge_schemas(base, ours, theirs)

    assert not result.has_conflicts
    names = {t.name for t in result.schema.tables}
    assert names == {"users", "orders", "invoices"}


def test_merge_only_ours_added_included() -> None:
    """Only ours adds a new table — it should be in the result."""
    base = _schema("users")
    ours = _schema("users", "payments")
    theirs = _schema("users")  # unchanged

    result = merge_schemas(base, ours, theirs)

    assert not result.has_conflicts
    assert any(t.name == "payments" for t in result.schema.tables)


def test_merge_only_theirs_added_included() -> None:
    """Only theirs adds a new table — it should be in the result."""
    base = _schema("users")
    ours = _schema("users")  # unchanged
    theirs = _schema("users", "notifications")

    result = merge_schemas(base, ours, theirs)

    assert not result.has_conflicts
    assert any(t.name == "notifications" for t in result.schema.tables)


# ---------------------------------------------------------------------------
# merge_schemas — deletions
# ---------------------------------------------------------------------------


def test_merge_table_deleted_on_theirs() -> None:
    """base has T, ours has T, theirs dropped T → theirs' deletion wins (T removed)."""
    base = _schema("users", "temp_data")
    ours = _schema("users", "temp_data")  # unchanged
    theirs = _schema("users")             # deleted temp_data

    result = merge_schemas(base, ours, theirs)

    assert not result.has_conflicts
    assert not any(t.name == "temp_data" for t in result.schema.tables)


def test_merge_table_deleted_on_ours() -> None:
    """base has T, ours dropped T, theirs has T → ours' deletion wins (T removed)."""
    base = _schema("users", "temp_data")
    ours = _schema("users")             # deleted temp_data
    theirs = _schema("users", "temp_data")  # unchanged

    result = merge_schemas(base, ours, theirs)

    assert not result.has_conflicts
    assert not any(t.name == "temp_data" for t in result.schema.tables)


# ---------------------------------------------------------------------------
# merge_schemas — conflicts
# ---------------------------------------------------------------------------


def test_merge_both_modified_same_table_creates_conflict() -> None:
    """Both sides modified the same existing table → conflict recorded."""
    base = _schema_with_tables([_table("users")])
    # ours adds an 'email' column
    ours = _schema_with_tables([
        _table("users", extra_cols=[_col("email", "string")])
    ])
    # theirs adds a 'phone' column
    theirs = _schema_with_tables([
        _table("users", extra_cols=[_col("phone", "string")])
    ])

    result = merge_schemas(base, ours, theirs)

    assert result.has_conflicts
    assert any("users" in msg for msg in result.conflicts)


def test_merge_conflict_keeps_ours() -> None:
    """On conflict, the ours version should be preserved in the result."""
    base = _schema_with_tables([_table("orders")])
    ours = _schema_with_tables([
        _table("orders", extra_cols=[_col("total", "decimal")])
    ])
    theirs = _schema_with_tables([
        _table("orders", extra_cols=[_col("amount", "decimal")])
    ])

    result = merge_schemas(base, ours, theirs)

    assert result.has_conflicts
    orders = next(t for t in result.schema.tables if t.name == "orders")
    col_names = {c.name for c in orders.columns}
    assert "total" in col_names     # ours preserved
    assert "amount" not in col_names  # theirs discarded


def test_merge_both_added_same_table_differently() -> None:
    """Both branches independently added a table with the same name but different def → conflict."""
    base = _schema("users")
    ours = _schema_with_tables([
        _table("users"),
        _table("events", extra_cols=[_col("type", "string")]),
    ])
    theirs = _schema_with_tables([
        _table("users"),
        _table("events", extra_cols=[_col("name", "string")]),  # different column
    ])

    result = merge_schemas(base, ours, theirs)

    assert result.has_conflicts
    assert any("events" in msg for msg in result.conflicts)


def test_merge_identical_modifications_no_conflict() -> None:
    """Both sides made the same change → auto-merged, no conflict."""
    base = _schema("users")
    modified = _schema_with_tables([
        _table("users", extra_cols=[_col("email", "string")])
    ])

    result = merge_schemas(base, modified, modified)

    assert not result.has_conflicts
    users = next(t for t in result.schema.tables if t.name == "users")
    assert any(c.name == "email" for c in users.columns)


# ---------------------------------------------------------------------------
# run_merge_driver
# ---------------------------------------------------------------------------


def _write_schema(path: Path, schema: AlterSchema) -> None:
    schema.save(path)


def test_run_merge_driver_clean_returns_0(tmp_path: Path) -> None:
    """Clean merge (no conflicts) returns exit code 0."""
    base_path = tmp_path / "base.alter"
    ours_path = tmp_path / "ours.alter"
    theirs_path = tmp_path / "theirs.alter"

    base = _schema("users")
    ours = _schema("users", "orders")
    theirs = _schema("users", "invoices")

    _write_schema(base_path, base)
    _write_schema(ours_path, ours)
    _write_schema(theirs_path, theirs)

    code = run_merge_driver(str(base_path), str(ours_path), str(theirs_path))
    assert code == 0


def test_run_merge_driver_writes_merged_to_ours_path(tmp_path: Path) -> None:
    """The merged result is written to the ours path (git convention)."""
    base_path = tmp_path / "base.alter"
    ours_path = tmp_path / "ours.alter"
    theirs_path = tmp_path / "theirs.alter"

    base = _schema("users")
    ours = _schema("users", "orders")
    theirs = _schema("users", "invoices")

    _write_schema(base_path, base)
    _write_schema(ours_path, ours)
    _write_schema(theirs_path, theirs)

    run_merge_driver(str(base_path), str(ours_path), str(theirs_path))

    merged = AlterSchema.load(ours_path)
    names = {t.name for t in merged.tables}
    assert "orders" in names
    assert "invoices" in names


def test_run_merge_driver_conflicts_returns_1(tmp_path: Path) -> None:
    """Conflicting changes return exit code 1."""
    base_path = tmp_path / "base.alter"
    ours_path = tmp_path / "ours.alter"
    theirs_path = tmp_path / "theirs.alter"

    base = _schema("users")
    ours = _schema_with_tables([
        _table("users"),
        _table("events", extra_cols=[_col("type", "string")]),
    ])
    theirs = _schema_with_tables([
        _table("users"),
        _table("events", extra_cols=[_col("name", "string")]),
    ])

    _write_schema(base_path, base)
    _write_schema(ours_path, ours)
    _write_schema(theirs_path, theirs)

    code = run_merge_driver(str(base_path), str(ours_path), str(theirs_path))
    assert code == 1


def test_run_merge_driver_invalid_json_returns_1(tmp_path: Path) -> None:
    """Malformed .alter file → returns 1 (no crash)."""
    base_path = tmp_path / "base.alter"
    ours_path = tmp_path / "ours.alter"
    theirs_path = tmp_path / "theirs.alter"

    base_path.write_text("not valid json {{")
    ours_path.write_text("{}")
    theirs_path.write_text("{}")

    code = run_merge_driver(str(base_path), str(ours_path), str(theirs_path))
    assert code == 1
