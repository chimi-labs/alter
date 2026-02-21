"""End-to-end smoke test — verifies the full core engine loop.

Covers: parse → propose → diff → export SQL → commit → generate code → round-trip.
If this test passes, the core engine is solid enough to move on to the canvas.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from alter.diff import diff_schemas
from alter.exporters.sql import export_sql
from alter.generators.base import get_generator
from alter.importers.sql import import_sql
from alter.parsers.base import get_parser
from alter.schema import AlterSchema, Column, Table
from alter.staging import StagingManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SAAS_APP_DIR = Path(__file__).parent.parent / "examples" / "saas-starter" / "app"


@pytest.fixture(scope="module")
def saas_schema() -> AlterSchema:
    """Parse the SaaS starter models.py and return the AlterSchema."""
    parser = get_parser("sqlmodel")
    result = parser.parse_directory(_SAAS_APP_DIR)
    assert result.schema.tables, "SaaS starter parser returned no tables"
    return result.schema


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------


def test_full_core_loop(saas_schema: AlterSchema, tmp_path: Path):
    """
    1. Parse the SaaS starter example → AlterSchema
    2. Propose changes: add a column and a new table
    3. Compute diff → verify correct change types detected
    4. Export proposed schema as SQL → verify it's non-empty and contains
       the expected CREATE TABLE statements
    5. Commit the change
    6. Generate SQLModel code from committed schema → verify round-trip
    """

    # ── Step 1: Schema already parsed in fixture ──────────────────────────
    assert len(saas_schema.tables) >= 1, "Expected at least one table"
    first_table_name = saas_schema.tables[0].name

    # ── Step 2: Set up staging manager and propose changes ─────────────────
    alter_path = tmp_path / "project.alter"
    saas_schema.save(alter_path)
    manager = StagingManager(alter_path)

    new_col = Column(name="bio", type="text")

    def add_bio_and_notifications(schema: AlterSchema) -> AlterSchema:
        # Add a column to the first table
        for t in schema.tables:
            if t.name == first_table_name:
                t.columns.append(new_col)
                break
        # Add a brand-new table
        schema.tables.append(
            Table(
                name="notifications",
                columns=[
                    Column(name="id", type="uuid", primary_key=True, nullable=False),
                    Column(name="message", type="text", nullable=False),
                    Column(name="read", type="bool", default="false"),
                ],
            )
        )
        return schema

    proposed = manager.propose(add_bio_and_notifications)
    assert manager.has_pending(), "Should have a pending proposal"

    # ── Step 3: Compute diff ───────────────────────────────────────────────
    changes = manager.get_diff()
    assert changes, "Diff should not be empty"

    change_types = {c.type for c in changes}
    assert "add_table" in change_types, f"Expected add_table in diff, got: {change_types}"
    assert "add_column" in change_types, f"Expected add_column in diff, got: {change_types}"

    # Verify the new table is in the diff
    add_table_change = next((c for c in changes if c.type == "add_table"), None)
    assert add_table_change is not None
    assert add_table_change.table == "notifications"

    # Verify the new column is in the diff
    add_col_change = next(
        (c for c in changes if c.type == "add_column" and c.column == "bio"), None
    )
    assert add_col_change is not None
    assert add_col_change.table == first_table_name

    # ── Step 4: Export proposed schema as SQL ─────────────────────────────
    proposed_schema = manager.effective_schema()
    sql_output = export_sql(proposed_schema)

    assert sql_output, "SQL export should not be empty"
    assert "CREATE TABLE notifications" in sql_output, (
        "SQL should contain CREATE TABLE for the new table"
    )
    assert f"CREATE TABLE {first_table_name}" in sql_output, (
        f"SQL should contain CREATE TABLE for {first_table_name}"
    )
    # bio column should appear in the SQL
    assert "bio" in sql_output, "New 'bio' column should appear in SQL"

    # ── Step 5: Commit ────────────────────────────────────────────────────
    manager.commit()
    assert not manager.has_pending(), "No pending proposal after commit"
    assert alter_path.exists(), ".alter file should be written to disk"

    committed = AlterSchema.load(alter_path)
    assert any(t.name == "notifications" for t in committed.tables), (
        "notifications table should be in committed schema"
    )

    # ── Step 6: Generate SQLModel code → round-trip ───────────────────────
    generator = get_generator("sqlmodel")
    generated_code = generator.generate_models(manager.current_schema)

    assert generated_code, "Generator should produce non-empty code"
    assert "class Notifications" in generated_code or "notifications" in generated_code, (
        "Generated code should reference the notifications table"
    )
    assert "bio" in generated_code, "Generated code should include the bio column"

    # Re-parse the generated code into a temp file
    gen_path = tmp_path / "generated_models.py"
    gen_path.write_text(generated_code, encoding="utf-8")

    re_parser = get_parser("sqlmodel")
    re_result = re_parser.parse_file(gen_path)

    # The re-parsed tables should include the original tables plus notifications
    re_table_names = {t.name for t in re_result}
    committed_table_names = {t.name for t in manager.current_schema.tables}

    # At minimum, all re-parsed tables should be in the committed schema
    for name in re_table_names:
        assert name in committed_table_names, (
            f"Re-parsed table '{name}' not found in committed schema"
        )

    # notifications must be in the re-parsed result
    assert "notifications" in re_table_names, (
        "notifications table should survive the round-trip"
    )


def test_staging_undo_redo_loop(tmp_path: Path):
    """Quick sanity: undo/redo works end-to-end with a real schema."""
    schema = AlterSchema(
        tables=[
            Table(
                name="users",
                columns=[Column(name="id", type="uuid", primary_key=True, nullable=False)],
            )
        ]
    )
    path = tmp_path / "undo_test.alter"
    schema.save(path)
    manager = StagingManager(path)

    # Propose two changes
    manager.propose(lambda s: _append_table(s, "orders"))
    manager.propose(lambda s: _append_table(s, "invoices"))
    assert _has_table(manager.proposed_schema, "invoices")

    # Undo twice → back to no proposal
    manager.undo()
    assert _has_table(manager.proposed_schema, "orders")
    assert not _has_table(manager.proposed_schema, "invoices")

    manager.undo()
    assert manager.proposed_schema is None

    # Redo → orders back
    manager.redo()
    assert _has_table(manager.proposed_schema, "orders")


# ---------------------------------------------------------------------------
# Helpers for smoke test
# ---------------------------------------------------------------------------


def _append_table(schema: AlterSchema, name: str) -> AlterSchema:
    schema.tables.append(
        Table(
            name=name,
            columns=[Column(name="id", type="uuid", primary_key=True, nullable=False)],
        )
    )
    return schema


def _has_table(schema: AlterSchema | None, name: str) -> bool:
    if schema is None:
        return False
    return any(t.name == name for t in schema.tables)
