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


# ---------------------------------------------------------------------------
# propose() atomicity — failed change_fn must not corrupt stacks
# ---------------------------------------------------------------------------


def _raise_fn(schema: AlterSchema) -> AlterSchema:
    """A change_fn that always raises."""
    raise ValueError("intentional failure")


class TestProposeAtomicity:

    def test_undo_stack_unchanged_after_failed_propose(self, tmp_path: Path) -> None:
        """A failing change_fn must not push a ghost entry onto the undo stack."""
        staging = StagingManager(tmp_path / "s.alter")
        staging.propose(_add_table("users"))
        depth_before = len(staging._undo_stack)

        with pytest.raises(ValueError):
            staging.propose(_raise_fn)

        assert len(staging._undo_stack) == depth_before

    def test_proposed_schema_unchanged_after_failed_propose(self, tmp_path: Path) -> None:
        """proposed_schema must be the same object after a failing change_fn."""
        staging = StagingManager(tmp_path / "s.alter")
        staging.propose(_add_table("users"))
        schema_before = staging.proposed_schema

        with pytest.raises(ValueError):
            staging.propose(_raise_fn)

        assert staging.proposed_schema is schema_before

    def test_redo_stack_preserved_after_failed_propose(self, tmp_path: Path) -> None:
        """A failing propose must NOT wipe redo history built up before it."""
        staging = StagingManager(tmp_path / "s.alter")
        staging.propose(_add_table("users"))
        staging.propose(_add_table("orders"))
        staging.undo()
        redo_depth_before = len(staging._redo_stack)
        assert redo_depth_before == 1  # sanity

        with pytest.raises(ValueError):
            staging.propose(_raise_fn)

        assert len(staging._redo_stack) == redo_depth_before, (
            "redo stack was wiped by a failing propose()"
        )

    def test_subsequent_valid_propose_succeeds_after_failed_one(self, tmp_path: Path) -> None:
        """After a failed propose, the next valid propose must work normally."""
        staging = StagingManager(tmp_path / "s.alter")

        with pytest.raises(ValueError):
            staging.propose(_raise_fn)

        staging.propose(_add_table("users"))
        assert staging.proposed_schema is not None
        assert any(t.name == "users" for t in staging.proposed_schema.tables)

    def test_undo_after_failed_propose_works_correctly(self, tmp_path: Path) -> None:
        """Undo after a mixed success/failure sequence must restore the right state."""
        staging = StagingManager(tmp_path / "s.alter")
        staging.propose(_add_table("users"))

        with pytest.raises(ValueError):
            staging.propose(_raise_fn)

        # Undo should restore to a schema WITHOUT 'users'
        reverted = staging.undo()
        table_names = {t.name for t in (reverted.tables if reverted else [])}
        assert "users" not in table_names

    def test_undo_stack_grows_only_on_successful_propose(self, tmp_path: Path) -> None:
        """Each successful propose adds exactly one entry; failures add none."""
        staging = StagingManager(tmp_path / "s.alter")

        staging.propose(_add_table("users"))
        assert len(staging._undo_stack) == 1

        for _ in range(3):
            with pytest.raises(ValueError):
                staging.propose(_raise_fn)

        assert len(staging._undo_stack) == 1  # still exactly one

        staging.propose(_add_table("orders"))
        assert len(staging._undo_stack) == 2
