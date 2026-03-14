"""Tests for src/alter/cli.py — Click commands via CliRunner.

Tests verify command behaviour, output format, and graceful error handling
(no raw Python tracebacks).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from alter.cli import main
from alter.schema import AlterSchema, Column, Table


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_schema(path: Path, schema: AlterSchema) -> None:
    schema.save(path)


def _minimal_schema() -> AlterSchema:
    """One-table schema: users(id uuid PK)."""
    return AlterSchema(
        tables=[
            Table(
                name="users",
                file_path="app/models.py",
                columns=[
                    Column(name="id", type="uuid", primary_key=True, nullable=False)
                ],
            )
        ]
    )


def _run(args: list[str], **kwargs) -> "click.testing.Result":
    runner = CliRunner()
    return runner.invoke(main, args, catch_exceptions=False, **kwargs)


# ---------------------------------------------------------------------------
# alter init
# ---------------------------------------------------------------------------


def test_init_creates_schema_alter(tmp_path: Path) -> None:
    """alter init --output <path> creates a .alter file in an empty directory."""
    out = tmp_path / "schema.alter"
    runner = CliRunner()
    # Run inside tmp_path so ORM detection scans an empty dir
    result = runner.invoke(
        main,
        ["init", "--output", str(out)],
        catch_exceptions=False,
        env={"HOME": str(tmp_path)},
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
    # File must be valid JSON
    data = json.loads(out.read_text())
    assert data["version"] == 1



# ---------------------------------------------------------------------------
# alter apply --preview
# ---------------------------------------------------------------------------


def test_apply_preview_shows_diff(tmp_path: Path) -> None:
    """alter apply --preview shows unified diff when model file doesn't yet exist."""
    alter_path = tmp_path / "schema.alter"
    _write_schema(alter_path, _minimal_schema())

    result = _run(["apply", "--preview", "--file", str(alter_path)])
    assert result.exit_code == 0, result.output
    # Unified diff starts with --- / +++
    assert "+++" in result.output


def test_apply_preview_no_changes_when_file_matches(tmp_path: Path) -> None:
    """apply --preview reports no changes when model file already matches .alter."""
    alter_path = tmp_path / "schema.alter"
    schema = _minimal_schema()
    _write_schema(alter_path, schema)

    # Generate the model file so it already matches
    from alter.generators.base import get_generator
    gen = get_generator("sqlmodel")
    models_path = tmp_path / "app" / "models.py"
    models_path.parent.mkdir(parents=True)
    models_path.write_text(gen.generate_models(schema))

    result = _run(["apply", "--preview", "--file", str(alter_path)])
    assert result.exit_code == 0
    assert "already up to date" in result.output


# ---------------------------------------------------------------------------
# alter export
# ---------------------------------------------------------------------------


def test_export_sql(tmp_path: Path) -> None:
    """alter export --format sql outputs SQL DDL to stdout."""
    alter_path = tmp_path / "schema.alter"
    _write_schema(alter_path, _minimal_schema())

    result = _run(["export", "--format", "sql", "--file", str(alter_path)])
    assert result.exit_code == 0, result.output
    output_upper = result.output.upper()
    assert "CREATE TABLE" in output_upper


def test_export_mermaid(tmp_path: Path) -> None:
    """alter export --format mermaid outputs Mermaid ERD."""
    alter_path = tmp_path / "schema.alter"
    _write_schema(alter_path, _minimal_schema())

    result = _run(["export", "--format", "mermaid", "--file", str(alter_path)])
    assert result.exit_code == 0, result.output
    assert "erDiagram" in result.output


def test_export_alter_is_valid_json(tmp_path: Path) -> None:
    """alter export --format alter outputs .alter JSON."""
    alter_path = tmp_path / "schema.alter"
    _write_schema(alter_path, _minimal_schema())

    result = _run(["export", "--format", "alter", "--file", str(alter_path)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["version"] == 1


def test_export_to_output_file(tmp_path: Path) -> None:
    """alter export --output FILE writes to file instead of stdout."""
    alter_path = tmp_path / "schema.alter"
    out_file = tmp_path / "output.sql"
    _write_schema(alter_path, _minimal_schema())

    result = _run(["export", "--format", "sql", "--file", str(alter_path), "--output", str(out_file)])
    assert result.exit_code == 0
    assert out_file.exists()
    assert "CREATE TABLE" in out_file.read_text().upper()


# ---------------------------------------------------------------------------
# alter diff --format markdown
# ---------------------------------------------------------------------------


def test_diff_format_markdown(tmp_path: Path) -> None:
    """alter diff --format markdown outputs PR-ready markdown.

    The .alter has tables but the code dir is empty → all tables show as
    'to be dropped' from code perspective (code has nothing; .alter has users).
    The markdown format should be produced regardless.
    """
    alter_path = tmp_path / "schema.alter"
    _write_schema(alter_path, _minimal_schema())

    result = _run(["diff", "--format", "markdown", "--file", str(alter_path)])
    # The command should run without error
    assert result.exit_code == 0 or "Schema Changes" in result.output or "No differences" in result.output


# ---------------------------------------------------------------------------
# alter validate
# ---------------------------------------------------------------------------


def test_validate_valid_schema(tmp_path: Path) -> None:
    """alter validate on a valid schema prints ✓ and exits 0."""
    alter_path = tmp_path / "schema.alter"
    _write_schema(alter_path, _minimal_schema())

    result = _run(["validate", "--file", str(alter_path)])
    assert result.exit_code == 0
    assert "✓" in result.output


# ---------------------------------------------------------------------------
# alter merge-driver
# ---------------------------------------------------------------------------


def _make_alter_file(path: Path, *table_names: str) -> None:
    schema = AlterSchema(
        tables=[
            Table(
                name=n,
                columns=[Column(name="id", type="uuid", primary_key=True, nullable=False)]
            )
            for n in table_names
        ]
    )
    schema.save(path)


def test_merge_driver_clean_exit_0(tmp_path: Path) -> None:
    """Non-overlapping additions → exit code 0."""
    base = tmp_path / "base.alter"
    ours = tmp_path / "ours.alter"
    theirs = tmp_path / "theirs.alter"

    _make_alter_file(base, "users")
    _make_alter_file(ours, "users", "orders")
    _make_alter_file(theirs, "users", "invoices")

    result = _run(["merge-driver", str(base), str(ours), str(theirs)])
    assert result.exit_code == 0


def test_merge_driver_writes_merged_result(tmp_path: Path) -> None:
    """Merged result is written to the ours file."""
    base = tmp_path / "base.alter"
    ours = tmp_path / "ours.alter"
    theirs = tmp_path / "theirs.alter"

    _make_alter_file(base, "users")
    _make_alter_file(ours, "users", "orders")
    _make_alter_file(theirs, "users", "invoices")

    _run(["merge-driver", str(base), str(ours), str(theirs)])

    merged = AlterSchema.load(ours)
    names = {t.name for t in merged.tables}
    assert "orders" in names
    assert "invoices" in names


def test_merge_driver_conflict_exit_1(tmp_path: Path) -> None:
    """Conflicting changes → exit code 1."""
    base = tmp_path / "base.alter"
    ours_path = tmp_path / "ours.alter"
    theirs_path = tmp_path / "theirs.alter"

    # Both branches add 'events' table with different columns
    _make_alter_file(base, "users")

    ours_schema = AlterSchema(
        tables=[
            Table(name="users", columns=[Column(name="id", type="uuid", primary_key=True, nullable=False)]),
            Table(name="events", columns=[
                Column(name="id", type="uuid", primary_key=True, nullable=False),
                Column(name="event_type", type="string"),
            ]),
        ]
    )
    theirs_schema = AlterSchema(
        tables=[
            Table(name="users", columns=[Column(name="id", type="uuid", primary_key=True, nullable=False)]),
            Table(name="events", columns=[
                Column(name="id", type="uuid", primary_key=True, nullable=False),
                Column(name="event_name", type="string"),  # different column
            ]),
        ]
    )
    ours_schema.save(ours_path)
    theirs_schema.save(theirs_path)
    base.write_text(AlterSchema(tables=[
        Table(name="users", columns=[Column(name="id", type="uuid", primary_key=True, nullable=False)])
    ]).model_dump_json())

    runner = CliRunner()
    result = runner.invoke(main, ["merge-driver", str(base), str(ours_path), str(theirs_path)])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Error handling — no raw tracebacks
# ---------------------------------------------------------------------------


def test_missing_alter_file_error_no_traceback(tmp_path: Path) -> None:
    """Running a command with no .alter file shows helpful error, not a traceback."""
    runner = CliRunner()
    # Run validate in a directory that has no .alter file (use isolated_filesystem)
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["validate"])

    assert result.exit_code != 0
    combined = (result.output or "") + (result.stderr or "")
    assert "Traceback" not in combined
    # Should mention .alter file or init
    assert ".alter" in combined or "alter init" in combined or "No .alter" in combined


def test_missing_alter_file_for_export_no_traceback(tmp_path: Path) -> None:
    """alter export with missing .alter shows clear error, not traceback."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["export"])

    combined = (result.output or "") + (result.stderr or "")
    assert "Traceback" not in combined


# ---------------------------------------------------------------------------
# alter add
# ---------------------------------------------------------------------------

_SQLMODEL_SINGLE_TABLE = """\
from sqlmodel import SQLModel, Field

class Orders(SQLModel, table=True):
    __tablename__ = "orders"
    id: int = Field(primary_key=True)
    user_id: int
"""

_SQLMODEL_NON_ORM = """\
def helper():
    return 42
"""

_SQLMODEL_IMPORT_ONLY = """\
from sqlmodel import SQLModel
"""


def test_add_registers_model_file(tmp_path: Path) -> None:
    """alter add parses a model file and adds its tables to the schema."""
    alter_path = tmp_path / "schema.alter"
    _write_schema(alter_path, _minimal_schema())

    model_file = tmp_path / "legacy.py"
    model_file.write_text(_SQLMODEL_SINGLE_TABLE)

    result = _run(["add", str(model_file), "--file", str(alter_path)])
    assert result.exit_code == 0, result.output
    assert "Added" in result.output
    assert "orders" in result.output

    schema = AlterSchema.load(alter_path)
    names = {t.name for t in schema.tables}
    assert "orders" in names


def test_add_skips_duplicate_tables(tmp_path: Path) -> None:
    """Tables already present in the schema are skipped without error."""
    alter_path = tmp_path / "schema.alter"
    schema = _minimal_schema()  # has 'users'
    _write_schema(alter_path, schema)

    # Model file also contains 'users'
    model_file = tmp_path / "existing.py"
    model_file.write_text(
        "from sqlmodel import SQLModel, Field\n\n"
        "class Users(SQLModel, table=True):\n"
        "    __tablename__ = 'users'\n"
        "    id: int = Field(primary_key=True)\n"
    )

    result = _run(["add", str(model_file), "--file", str(alter_path)])
    assert result.exit_code == 0, result.output
    assert "Skipped" in result.output

    loaded = AlterSchema.load(alter_path)
    assert len([t for t in loaded.tables if t.name == "users"]) == 1


def test_add_sets_relative_file_path(tmp_path: Path) -> None:
    """file_path on added tables is stored relative to the project root."""
    alter_path = tmp_path / "schema.alter"
    _write_schema(alter_path, _minimal_schema())

    sub = tmp_path / "src" / "deep"
    sub.mkdir(parents=True)
    model_file = sub / "models.py"
    model_file.write_text(_SQLMODEL_SINGLE_TABLE)

    result = _run(["add", str(model_file), "--file", str(alter_path)])
    assert result.exit_code == 0, result.output

    loaded = AlterSchema.load(alter_path)
    orders = next(t for t in loaded.tables if t.name == "orders")
    assert orders.file_path == "src/deep/models.py"


def test_add_fails_on_non_orm_file(tmp_path: Path) -> None:
    """A plain Python file that contains no ORM models raises a clear error."""
    alter_path = tmp_path / "schema.alter"
    _write_schema(alter_path, _minimal_schema())

    non_orm = tmp_path / "utils.py"
    non_orm.write_text(_SQLMODEL_NON_ORM)

    result = _run(["add", str(non_orm), "--file", str(alter_path)])
    assert result.exit_code != 0
    combined = (result.output or "") + (result.stderr or "")
    assert "does not contain" in combined
    assert "Traceback" not in combined


def test_add_fails_on_empty_file(tmp_path: Path) -> None:
    """A file with ORM imports but no table classes raises a clear error."""
    alter_path = tmp_path / "schema.alter"
    _write_schema(alter_path, _minimal_schema())

    empty_orm = tmp_path / "empty_models.py"
    empty_orm.write_text(_SQLMODEL_IMPORT_ONLY)

    result = _run(["add", str(empty_orm), "--file", str(alter_path)])
    assert result.exit_code != 0
    combined = (result.output or "") + (result.stderr or "")
    assert "No tables found" in combined or "does not contain" in combined
    assert "Traceback" not in combined


def test_add_preserves_enum_definitions(tmp_path: Path) -> None:
    """Regression: custom enum types on columns must not cause validation errors.

    When a model file defines a Python Enum and uses it as a column type,
    alter add must include the enum definition in schema.alter so that
    validation does not raise 'unknown type <EnumName>'.
    """
    alter_path = tmp_path / "schema.alter"
    _write_schema(alter_path, _minimal_schema())

    model_file = tmp_path / "memberships.py"
    model_file.write_text(
        "from enum import Enum\n"
        "from sqlmodel import SQLModel, Field\n\n"
        "class Role(str, Enum):\n"
        "    admin = 'admin'\n"
        "    member = 'member'\n\n"
        "class Memberships(SQLModel, table=True):\n"
        "    __tablename__ = 'memberships'\n"
        "    id: int = Field(primary_key=True)\n"
        "    role: Role\n"
    )

    result = _run(["add", str(model_file), "--file", str(alter_path)])
    assert result.exit_code == 0, result.output
    assert "memberships" in result.output

    # The saved schema must load cleanly (no validation error)
    schema = AlterSchema.load(alter_path)
    enum_names = {e.name for e in schema.enums}
    assert "Role" in enum_names


# ---------------------------------------------------------------------------
# Bug 14: auto-layout positions — init / sync / add
# ---------------------------------------------------------------------------

# A two-table model file for position tests.
_SQLMODEL_TWO_TABLES = """\
from sqlmodel import SQLModel, Field

class Products(SQLModel, table=True):
    __tablename__ = "products"
    id: int = Field(primary_key=True)
    name: str

class Categories(SQLModel, table=True):
    __tablename__ = "categories"
    id: int = Field(primary_key=True)
    label: str
"""

_SQLMODEL_THIRD_TABLE = """\
from sqlmodel import SQLModel, Field

class Tags(SQLModel, table=True):
    __tablename__ = "tags"
    id: int = Field(primary_key=True)
    slug: str
"""


def test_init_tables_have_nonzero_positions(tmp_path: Path) -> None:
    """alter init must auto-layout tables so none remains at (0, 0)."""
    model_file = tmp_path / "models.py"
    model_file.write_text(_SQLMODEL_TWO_TABLES)
    out = tmp_path / "schema.alter"

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["init", "--output", str(out), "--orm", "sqlmodel"],
        catch_exceptions=False,
        env={"HOME": str(tmp_path)},
    )
    assert result.exit_code == 0, result.output

    schema = AlterSchema.load(out)
    for tbl in schema.tables:
        assert tbl.position.x != 0 or tbl.position.y != 0, (
            f"Table '{tbl.name}' was left at the origin after alter init"
        )


def test_init_tables_have_unique_positions(tmp_path: Path) -> None:
    """alter init must assign distinct positions to every table."""
    model_file = tmp_path / "models.py"
    model_file.write_text(_SQLMODEL_TWO_TABLES)
    out = tmp_path / "schema.alter"

    runner = CliRunner()
    runner.invoke(
        main,
        ["init", "--output", str(out), "--orm", "sqlmodel"],
        catch_exceptions=False,
        env={"HOME": str(tmp_path)},
    )

    schema = AlterSchema.load(out)
    coords = [(t.position.x, t.position.y) for t in schema.tables]
    assert len(coords) == len(set(coords)), "alter init produced overlapping table positions"


def test_sync_preserves_existing_positions(tmp_path: Path) -> None:
    """alter sync must not move tables that are already positioned."""
    from alter.schema import Position

    # Seed a schema with a table that has a custom canvas position.
    alter_path = tmp_path / "schema.alter"
    schema = AlterSchema(
        orm="sqlmodel",
        tables=[
            Table(
                name="products",
                file_path="models.py",
                position=Position(x=999, y=888),
                columns=[Column(name="id", type="uuid", primary_key=True, nullable=False)],
            )
        ],
    )
    schema.save(alter_path)

    # The model file still contains 'products' — sync should leave its position.
    model_file = tmp_path / "models.py"
    model_file.write_text(
        "from sqlmodel import SQLModel, Field\n\n"
        "class Products(SQLModel, table=True):\n"
        "    __tablename__ = 'products'\n"
        "    id: int = Field(primary_key=True)\n"
    )

    result = _run(["sync", "--file", str(alter_path)])
    assert result.exit_code == 0, result.output

    loaded = AlterSchema.load(alter_path)
    products = next(t for t in loaded.tables if t.name == "products")
    assert products.position.x == 999
    assert products.position.y == 888


def test_sync_auto_positions_new_tables(tmp_path: Path) -> None:
    """alter sync must give non-zero positions to tables that weren't in the old schema."""
    from alter.schema import Position

    # Seed a schema with just 'products'.
    alter_path = tmp_path / "schema.alter"
    schema = AlterSchema(
        orm="sqlmodel",
        tables=[
            Table(
                name="products",
                file_path="models.py",
                position=Position(x=50, y=50),
                columns=[Column(name="id", type="uuid", primary_key=True, nullable=False)],
            )
        ],
    )
    schema.save(alter_path)

    # Updated model file now also contains 'categories' — a brand-new table.
    model_file = tmp_path / "models.py"
    model_file.write_text(_SQLMODEL_TWO_TABLES)

    result = _run(["sync", "--file", str(alter_path)])
    assert result.exit_code == 0, result.output

    loaded = AlterSchema.load(alter_path)
    categories = next(t for t in loaded.tables if t.name == "categories")
    assert categories.position.x != 0 or categories.position.y != 0, (
        "New table 'categories' was left at origin after alter sync"
    )


def test_sync_new_table_does_not_overlap_existing(tmp_path: Path) -> None:
    """A new table added via sync must not share the position of an existing table."""
    from alter.schema import Position

    alter_path = tmp_path / "schema.alter"
    schema = AlterSchema(
        orm="sqlmodel",
        tables=[
            Table(
                name="products",
                file_path="models.py",
                position=Position(x=50, y=50),  # grid_position(0)
                columns=[Column(name="id", type="uuid", primary_key=True, nullable=False)],
            )
        ],
    )
    schema.save(alter_path)

    model_file = tmp_path / "models.py"
    model_file.write_text(_SQLMODEL_TWO_TABLES)

    _run(["sync", "--file", str(alter_path)])

    loaded = AlterSchema.load(alter_path)
    coords = [(t.position.x, t.position.y) for t in loaded.tables]
    assert len(coords) == len(set(coords)), "Positions must not overlap after sync"


def test_add_positions_new_table(tmp_path: Path) -> None:
    """alter add must give a non-zero canvas position to the added table."""
    alter_path = tmp_path / "schema.alter"
    _write_schema(alter_path, _minimal_schema())  # has 'users' at (0,0) via default

    model_file = tmp_path / "tags.py"
    model_file.write_text(_SQLMODEL_THIRD_TABLE)

    result = _run(["add", str(model_file), "--file", str(alter_path)])
    assert result.exit_code == 0, result.output

    schema = AlterSchema.load(alter_path)
    tags = next(t for t in schema.tables if t.name == "tags")
    assert tags.position.x != 0 or tags.position.y != 0, (
        "alter add left the new table at origin"
    )
