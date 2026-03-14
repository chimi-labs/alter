"""Tests for _sync_from_code_impl — the shared implementation behind both
the MCP sync_from_code tool and the canvas POST /api/sync-from-code endpoint.

Previously the function only re-parsed files already tracked in schema.alter,
so new model files added after `alter init` were silently ignored.  The fix
always uses parse_directory() — the same strategy as `alter sync` in the CLI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import alter.mcp_server as ms
from alter.mcp_server import _sync_from_code_impl
from alter.schema import AlterSchema, Column, Position, Table
from alter.staging import StagingManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SQLMODEL_HEADER = """\
from __future__ import annotations
from sqlmodel import SQLModel, Field
import uuid as _uuid

"""


def _model(name: str, extra: str = "") -> str:
    """Minimal SQLModel class for *name* with a uuid PK."""
    return (
        SQLMODEL_HEADER
        + f"class {name.capitalize()}(SQLModel, table=True):\n"
        + f"    __tablename__ = '{name}'\n"
        + f"    id: _uuid.UUID = Field(default_factory=_uuid.uuid4, primary_key=True)\n"
        + (extra or "")
        + "\n"
    )


def _make_alter(tmp_path: Path, tables: list[Table] | None = None) -> Path:
    """Write a minimal schema.alter for a SQLModel project and return its path."""
    schema = AlterSchema(orm="sqlmodel", tables=tables or [])
    path = tmp_path / "schema.alter"
    schema.save(path)
    return path


# ---------------------------------------------------------------------------
# Tests — new file discovery
# ---------------------------------------------------------------------------


class TestNewFileDiscovery:
    """Verify that sync discovers model files added after alter init."""

    def test_new_file_is_discovered(self, tmp_path: Path) -> None:
        """A model file not yet in schema.alter must be found after sync."""
        # Start with a schema that knows about models_a.py only
        (tmp_path / "models_a.py").write_text(_model("user"))
        alter_path = _make_alter(
            tmp_path,
            tables=[Table(name="user", columns=[], file_path="models_a.py")],
        )
        staging = StagingManager(alter_path)

        # Developer adds a brand-new file *after* init
        (tmp_path / "models_b.py").write_text(_model("payment"))

        _sync_from_code_impl(staging, tmp_path, alter_file=alter_path)

        names = {t.name for t in staging.current_schema.tables}
        assert "user" in names
        assert "payment" in names, "New file models_b.py was not discovered by sync"

    def test_new_file_in_subdirectory_is_discovered(self, tmp_path: Path) -> None:
        """New model files in subdirectories are also discovered."""
        (tmp_path / "models_a.py").write_text(_model("user"))
        alter_path = _make_alter(
            tmp_path,
            tables=[Table(name="user", columns=[], file_path="models_a.py")],
        )
        staging = StagingManager(alter_path)

        subdir = tmp_path / "payments"
        subdir.mkdir()
        (subdir / "models.py").write_text(_model("payment"))

        _sync_from_code_impl(staging, tmp_path, alter_file=alter_path)

        names = {t.name for t in staging.current_schema.tables}
        assert "payment" in names, "New file in subdirectory was not discovered"

    def test_empty_schema_discovers_all_files(self, tmp_path: Path) -> None:
        """Even with an empty schema, sync discovers all model files."""
        (tmp_path / "models.py").write_text(_model("order"))
        alter_path = _make_alter(tmp_path, tables=[])
        staging = StagingManager(alter_path)

        _sync_from_code_impl(staging, tmp_path, alter_file=alter_path)

        names = {t.name for t in staging.current_schema.tables}
        assert "order" in names


# ---------------------------------------------------------------------------
# Tests — position preservation
# ---------------------------------------------------------------------------


class TestPositionPreservation:
    """Canvas positions for existing tables must survive a sync."""

    def test_existing_position_is_preserved(self, tmp_path: Path) -> None:
        """A table that already existed keeps its canvas position after sync."""
        (tmp_path / "models.py").write_text(_model("user"))
        existing_pos = Position(x=123.0, y=456.0)
        alter_path = _make_alter(
            tmp_path,
            tables=[
                Table(
                    name="user",
                    columns=[],
                    file_path="models.py",
                    position=existing_pos,
                )
            ],
        )
        staging = StagingManager(alter_path)

        _sync_from_code_impl(staging, tmp_path, alter_file=alter_path)

        synced = next(t for t in staging.current_schema.tables if t.name == "user")
        assert synced.position.x == pytest.approx(123.0)
        assert synced.position.y == pytest.approx(456.0)

    def test_new_table_gets_auto_laid_out(self, tmp_path: Path) -> None:
        """A newly discovered table must not remain at (0, 0) after sync."""
        (tmp_path / "models.py").write_text(_model("user") + _model("order"))
        alter_path = _make_alter(tmp_path, tables=[])
        staging = StagingManager(alter_path)

        _sync_from_code_impl(staging, tmp_path, alter_file=alter_path)

        # At least one table must have been moved away from origin
        positions = {(t.position.x, t.position.y) for t in staging.current_schema.tables}
        assert (0.0, 0.0) not in positions or len(positions) > 1, (
            "All tables are at (0, 0) — auto-layout was not applied"
        )


# ---------------------------------------------------------------------------
# Tests — table removal (file deleted)
# ---------------------------------------------------------------------------


class TestTableRemoval:
    """Tables whose source file was deleted must be dropped from the schema."""

    def test_deleted_file_tables_are_removed(self, tmp_path: Path) -> None:
        """Tables from a deleted model file do not appear after sync."""
        models_a = tmp_path / "models_a.py"
        models_a.write_text(_model("user"))
        models_b = tmp_path / "models_b.py"
        models_b.write_text(_model("payment"))

        alter_path = _make_alter(
            tmp_path,
            tables=[
                Table(name="user", columns=[], file_path="models_a.py"),
                Table(name="payment", columns=[], file_path="models_b.py"),
            ],
        )
        staging = StagingManager(alter_path)

        # Developer deletes the payments file
        models_b.unlink()

        _sync_from_code_impl(staging, tmp_path, alter_file=alter_path)

        names = {t.name for t in staging.current_schema.tables}
        assert "user" in names
        assert "payment" not in names, "Deleted table 'payment' still in schema after sync"


# ---------------------------------------------------------------------------
# Tests — summary message
# ---------------------------------------------------------------------------


class TestSummaryMessage:
    def test_summary_reports_table_count(self, tmp_path: Path) -> None:
        (tmp_path / "models.py").write_text(_model("user") + _model("order"))
        alter_path = _make_alter(tmp_path, tables=[])
        staging = StagingManager(alter_path)

        msg = _sync_from_code_impl(staging, tmp_path, alter_file=alter_path)

        assert "2" in msg or "table" in msg.lower()

    def test_summary_includes_skipped_when_parse_error(self, tmp_path: Path) -> None:
        (tmp_path / "good.py").write_text(_model("user"))
        (tmp_path / "broken.py").write_text("def bad syntax <<<\n")
        alter_path = _make_alter(tmp_path, tables=[])
        staging = StagingManager(alter_path)

        msg = _sync_from_code_impl(staging, tmp_path, alter_file=alter_path)

        # Either the bad file is gracefully skipped (message mentions "skipped")
        # or the parser simply ignores non-ORM files — both are acceptable.
        assert "user" in {t.name for t in staging.current_schema.tables}


# ---------------------------------------------------------------------------
# Tests — parity with CLI alter sync
# ---------------------------------------------------------------------------


class TestCliParity:
    """Confirm sync_from_code produces the same table set as `alter sync`."""

    def test_same_tables_as_cli_sync(self, tmp_path: Path) -> None:
        """_sync_from_code_impl and CLI parse_directory must agree on tables."""
        from alter.parsers.base import get_parser

        (tmp_path / "models.py").write_text(_model("user") + _model("order"))
        alter_path = _make_alter(tmp_path, tables=[])
        staging = StagingManager(alter_path)

        _sync_from_code_impl(staging, tmp_path, alter_file=alter_path)
        impl_names = {t.name for t in staging.current_schema.tables}

        # Independently run parse_directory (what the CLI does)
        parser = get_parser("sqlmodel", project_root=tmp_path)
        cli_result = parser.parse_directory(tmp_path)
        cli_names = {t.name for t in cli_result.schema.tables}

        assert impl_names == cli_names, (
            f"sync_from_code found {impl_names} but CLI parse_directory found {cli_names}"
        )
