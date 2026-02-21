"""Tests for SQL migration preview (pending-changes → SQL generation).

These tests cover the ``preview_migration`` MCP tool and the underlying
``_migration_sql`` helper in ``alter.canvas.server``.  No Alembic config,
no database connection needed.
"""

from __future__ import annotations

import copy
from pathlib import Path

from alter.canvas.server import _migration_sql
from alter.schema import AlterSchema, Column, Table
from alter.staging import StagingManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _staging_with_table(tmp_path: Path) -> StagingManager:
    """Return a staging manager that has an 'orders' table in the proposed schema."""
    path = tmp_path / "schema.alter"
    staging = StagingManager(path)

    def _add_orders(schema: AlterSchema) -> AlterSchema:
        s = copy.deepcopy(schema)
        s.tables.append(
            Table(
                name="orders",
                columns=[
                    Column(name="id", type="uuid", primary_key=True, nullable=False),
                    Column(name="total", type="decimal"),
                ],
            )
        )
        return s

    staging.propose(_add_orders)
    return staging


# ---------------------------------------------------------------------------
# _migration_sql / preview_migration
# ---------------------------------------------------------------------------


def test_migration_sql_no_pending(tmp_path: Path) -> None:
    """No pending changes → empty string."""
    path = tmp_path / "schema.alter"
    staging = StagingManager(path)
    result = _migration_sql(staging)
    assert result == "" or result.strip() == ""


def test_migration_sql_with_added_table(tmp_path: Path) -> None:
    """Proposed table addition → SQL contains CREATE TABLE."""
    staging = _staging_with_table(tmp_path)
    result = _migration_sql(staging)
    assert "CREATE TABLE" in result.upper()


def test_migration_sql_returns_string(tmp_path: Path) -> None:
    """Always returns a string (never None)."""
    path = tmp_path / "schema.alter"
    staging = StagingManager(path)
    result = _migration_sql(staging)
    assert isinstance(result, str)
