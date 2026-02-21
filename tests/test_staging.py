"""Tests for StagingManager (alter.staging)."""

from __future__ import annotations

from pathlib import Path

import pytest

from alter.schema import AlterSchema, Column, Table
from alter.staging import StagingManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _simple_schema() -> AlterSchema:
    return AlterSchema(
        tables=[
            Table(
                name="users",
                columns=[Column(name="id", type="uuid", primary_key=True, nullable=False)],
            )
        ]
    )


def _add_table(name: str):
    """Return a change_fn that appends a new table."""

    def _fn(schema: AlterSchema) -> AlterSchema:
        schema.tables.append(
            Table(
                name=name,
                columns=[Column(name="id", type="uuid", primary_key=True, nullable=False)],
            )
        )
        return schema

    return _fn


def _add_column(table_name: str, col_name: str, col_type: str = "string"):
    """Return a change_fn that adds a column to a table."""

    def _fn(schema: AlterSchema) -> AlterSchema:
        for t in schema.tables:
            if t.name == table_name:
                t.columns.append(Column(name=col_name, type=col_type))
        return schema

    return _fn


@pytest.fixture
def empty_staging(tmp_path: Path) -> StagingManager:
    """StagingManager backed by a non-existent file (starts with empty schema)."""
    return StagingManager(tmp_path / "test.alter")


@pytest.fixture
def preloaded_staging(tmp_path: Path) -> StagingManager:
    """StagingManager backed by a pre-saved schema file."""
    path = tmp_path / "schema.alter"
    _simple_schema().save(path)
    return StagingManager(path)


# ---------------------------------------------------------------------------
# has_pending
# ---------------------------------------------------------------------------


def test_has_pending_false_initially(empty_staging: StagingManager):
    assert empty_staging.has_pending() is False


def test_has_pending_true_after_propose(empty_staging: StagingManager):
    empty_staging.propose(lambda s: s)
    assert empty_staging.has_pending() is True


def test_has_pending_false_after_discard(empty_staging: StagingManager):
    empty_staging.propose(lambda s: s)
    empty_staging.discard()
    assert empty_staging.has_pending() is False


def test_has_pending_false_after_commit(preloaded_staging: StagingManager):
    preloaded_staging.propose(lambda s: s)
    preloaded_staging.commit()
    assert preloaded_staging.has_pending() is False


# ---------------------------------------------------------------------------
# propose → commit → disk write
# ---------------------------------------------------------------------------


def test_commit_writes_to_disk(tmp_path: Path):
    path = tmp_path / "schema.alter"
    manager = StagingManager(path)

    manager.propose(_add_table("orders"))
    manager.commit()

    assert path.exists()
    loaded = AlterSchema.load(path)
    assert any(t.name == "orders" for t in loaded.tables)


def test_commit_updates_current_schema(preloaded_staging: StagingManager):
    preloaded_staging.propose(_add_table("orders"))
    preloaded_staging.commit()

    assert any(t.name == "orders" for t in preloaded_staging.current_schema.tables)
    assert preloaded_staging.proposed_schema is None


def test_commit_clears_both_stacks(preloaded_staging: StagingManager):
    preloaded_staging.propose(_add_table("orders"))
    preloaded_staging.propose(_add_table("invoices"))
    preloaded_staging.commit()

    assert preloaded_staging._undo_stack == []
    assert preloaded_staging._redo_stack == []


# ---------------------------------------------------------------------------
# propose → discard
# ---------------------------------------------------------------------------


def test_discard_clears_proposal(preloaded_staging: StagingManager):
    preloaded_staging.propose(_add_table("orders"))
    preloaded_staging.discard()

    assert preloaded_staging.proposed_schema is None


def test_discard_does_not_affect_current_schema(preloaded_staging: StagingManager):
    original_tables = [t.name for t in preloaded_staging.current_schema.tables]
    preloaded_staging.propose(_add_table("orders"))
    preloaded_staging.discard()

    assert [t.name for t in preloaded_staging.current_schema.tables] == original_tables


def test_discard_clears_both_stacks(preloaded_staging: StagingManager):
    preloaded_staging.propose(_add_table("orders"))
    preloaded_staging.discard()

    assert preloaded_staging._undo_stack == []
    assert preloaded_staging._redo_stack == []


# ---------------------------------------------------------------------------
# Stacked proposals
# ---------------------------------------------------------------------------


def test_stacked_proposals_accumulate(preloaded_staging: StagingManager):
    """Three successive proposals produce cumulative changes."""
    preloaded_staging.propose(_add_table("orders"))
    preloaded_staging.propose(_add_table("invoices"))
    preloaded_staging.propose(_add_table("payments"))

    schema = preloaded_staging.proposed_schema
    assert schema is not None
    table_names = {t.name for t in schema.tables}
    assert {"orders", "invoices", "payments"}.issubset(table_names)


def test_stacked_proposals_fill_undo_stack(preloaded_staging: StagingManager):
    preloaded_staging.propose(_add_table("orders"))
    preloaded_staging.propose(_add_table("invoices"))

    assert len(preloaded_staging._undo_stack) == 2


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------


def test_undo_reverts_last_proposal(preloaded_staging: StagingManager):
    preloaded_staging.propose(_add_table("orders"))
    preloaded_staging.propose(_add_table("invoices"))
    preloaded_staging.undo()

    schema = preloaded_staging.proposed_schema
    assert schema is not None
    table_names = {t.name for t in schema.tables}
    assert "orders" in table_names
    assert "invoices" not in table_names


def test_undo_to_initial_state_clears_proposed(preloaded_staging: StagingManager):
    """Undoing back to current_schema sets proposed_schema to None."""
    preloaded_staging.propose(_add_table("orders"))
    result = preloaded_staging.undo()

    assert result is None
    assert preloaded_staging.proposed_schema is None
    assert preloaded_staging.has_pending() is False


def test_undo_pushes_to_redo_stack(preloaded_staging: StagingManager):
    preloaded_staging.propose(_add_table("orders"))
    preloaded_staging.undo()

    assert len(preloaded_staging._redo_stack) == 1


def test_undo_on_empty_stack_returns_none(empty_staging: StagingManager):
    result = empty_staging.undo()
    assert result is None


# ---------------------------------------------------------------------------
# Redo
# ---------------------------------------------------------------------------


def test_redo_reapplies_undone_proposal(preloaded_staging: StagingManager):
    preloaded_staging.propose(_add_table("orders"))
    preloaded_staging.undo()
    preloaded_staging.redo()

    schema = preloaded_staging.proposed_schema
    assert schema is not None
    assert any(t.name == "orders" for t in schema.tables)


def test_redo_on_empty_stack_returns_none(empty_staging: StagingManager):
    result = empty_staging.redo()
    assert result is None


# ---------------------------------------------------------------------------
# Undo + new proposal clears redo stack
# ---------------------------------------------------------------------------


def test_new_proposal_after_undo_clears_redo(preloaded_staging: StagingManager):
    preloaded_staging.propose(_add_table("orders"))
    preloaded_staging.undo()                       # redo_stack = [orders_change]
    preloaded_staging.propose(_add_table("invoices"))  # redo_stack should be cleared

    assert preloaded_staging._redo_stack == []


# ---------------------------------------------------------------------------
# get_diff
# ---------------------------------------------------------------------------


def test_get_diff_empty_when_no_pending(preloaded_staging: StagingManager):
    assert preloaded_staging.get_diff() == []


def test_get_diff_detects_changes(preloaded_staging: StagingManager):
    preloaded_staging.propose(_add_table("orders"))
    changes = preloaded_staging.get_diff()

    assert any(c.type == "add_table" and c.table == "orders" for c in changes)


# ---------------------------------------------------------------------------
# effective_schema
# ---------------------------------------------------------------------------


def test_effective_schema_returns_current_when_no_pending(preloaded_staging: StagingManager):
    eff = preloaded_staging.effective_schema()
    assert eff is preloaded_staging.current_schema


def test_effective_schema_returns_proposed_when_pending(preloaded_staging: StagingManager):
    preloaded_staging.propose(_add_table("orders"))
    eff = preloaded_staging.effective_schema()
    assert eff is preloaded_staging.proposed_schema
