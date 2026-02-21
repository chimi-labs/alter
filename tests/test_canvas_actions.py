"""Tests for the canvas /api/apply-to-code and /api/sync-from-code endpoints.

Tests call _apply_to_code_impl and _sync_from_code_impl directly, bypassing
HTTP transport, following the same pattern used in test_mcp_server.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alter.mcp_server import _apply_to_code_impl, _sync_from_code_impl
from alter.schema import AlterSchema, Column, Position, Table
from alter.staging import StagingManager


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_SQLMODEL_USER = """\
from __future__ import annotations
import uuid
from sqlmodel import SQLModel, Field

class Users(SQLModel, table=True):
    __tablename__ = "users"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    email: str = Field(unique=True)
"""

_SQLMODEL_WITH_HELPER = """\
from __future__ import annotations
import uuid
from sqlmodel import SQLModel, Field

class Users(SQLModel, table=True):
    __tablename__ = "users"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    email: str = Field(unique=True)

def get_display_name(user: Users) -> str:
    \"\"\"Helper: must survive alter apply.\"\"\"
    return user.email.split("@")[0]
"""


def _make_schema(tmp_path: Path, tables: list[Table], file_path: str = "app/models.py") -> tuple[Path, StagingManager]:
    """Create a schema.alter + StagingManager for a project rooted at tmp_path."""
    for t in tables:
        if not t.file_path:
            t.file_path = file_path
    schema = AlterSchema(orm="sqlmodel", tables=tables)
    alter_file = tmp_path / "schema.alter"
    schema.save(alter_file)
    staging = StagingManager(alter_file)
    return alter_file, staging


def _users_table(file_path: str = "app/models.py") -> Table:
    return Table(
        name="users",
        file_path=file_path,
        columns=[
            Column(name="id", type="uuid", primary_key=True),
            Column(name="email", type="string", unique=True),
        ],
    )


# ---------------------------------------------------------------------------
# _apply_to_code_impl tests
# ---------------------------------------------------------------------------


def test_apply_to_code_preview_returns_diff(tmp_path: Path) -> None:
    """Preview mode returns a unified diff without writing any files."""
    _, staging = _make_schema(tmp_path, [_users_table()])

    result = _apply_to_code_impl(staging, tmp_path, preview=True)

    assert "+++" in result, "Expected unified diff output in preview mode"
    assert "users" in result.lower()
    assert not (tmp_path / "app" / "models.py").exists(), "Preview must not write files"


def test_apply_to_code_writes_model_file(tmp_path: Path) -> None:
    """apply writes the model file to the expected path."""
    _, staging = _make_schema(tmp_path, [_users_table()])

    result = _apply_to_code_impl(staging, tmp_path, preview=False)

    models_file = tmp_path / "app" / "models.py"
    assert models_file.exists(), "models.py should have been created"
    content = models_file.read_text()
    assert "class Users" in content
    assert "email" in content
    assert "Applied to:" in result or "app/models.py" in result


def test_apply_to_code_no_changes(tmp_path: Path) -> None:
    """When model file already matches schema, returns up-to-date message."""
    _, staging = _make_schema(tmp_path, [_users_table()])

    # First apply writes the file
    _apply_to_code_impl(staging, tmp_path, preview=False)
    # Second apply finds nothing changed
    result = _apply_to_code_impl(staging, tmp_path, preview=False)

    assert "up to date" in result.lower()


def test_apply_to_code_creates_parent_dirs(tmp_path: Path) -> None:
    """apply creates intermediate directories for nested file_path values."""
    table = _users_table(file_path="src/db/models/users.py")
    _, staging = _make_schema(tmp_path, [table])

    _apply_to_code_impl(staging, tmp_path, preview=False)

    assert (tmp_path / "src" / "db" / "models" / "users.py").exists()


def test_apply_to_code_multifile(tmp_path: Path) -> None:
    """Tables with different file_path values are written to separate files."""
    users = _users_table(file_path="models/users.py")
    products = Table(
        name="products",
        file_path="models/products.py",
        columns=[Column(name="id", type="uuid", primary_key=True)],
    )
    _, staging = _make_schema(tmp_path, [users, products])

    _apply_to_code_impl(staging, tmp_path, preview=False)

    assert (tmp_path / "models" / "users.py").exists()
    assert (tmp_path / "models" / "products.py").exists()
    users_text = (tmp_path / "models" / "users.py").read_text()
    products_text = (tmp_path / "models" / "products.py").read_text()
    assert "users" in users_text
    assert "products" in products_text
    assert "products" not in users_text
    assert "users" not in products_text


# ---------------------------------------------------------------------------
# _sync_from_code_impl tests
# ---------------------------------------------------------------------------


def test_sync_from_code_updates_schema(tmp_path: Path) -> None:
    """Sync reads model files and updates the staging schema."""
    models_file = tmp_path / "app" / "models.py"
    models_file.parent.mkdir(parents=True)
    models_file.write_text(_SQLMODEL_USER)

    alter_file, staging = _make_schema(tmp_path, [_users_table()])

    result = _sync_from_code_impl(staging, tmp_path, alter_file=alter_file)

    assert "Synced" in result
    table_names = {t.name for t in staging.current_schema.tables}
    assert "users" in table_names


def test_sync_from_code_preserves_positions(tmp_path: Path) -> None:
    """Sync preserves existing table positions from the .alter schema."""
    table = _users_table()
    table.position = Position(x=123, y=456)
    models_file = tmp_path / "app" / "models.py"
    models_file.parent.mkdir(parents=True)
    models_file.write_text(_SQLMODEL_USER)

    alter_file, staging = _make_schema(tmp_path, [table])

    _sync_from_code_impl(staging, tmp_path, alter_file=alter_file)

    synced_table = next(t for t in staging.current_schema.tables if t.name == "users")
    assert synced_table.position is not None
    assert synced_table.position.x == 123
    assert synced_table.position.y == 456


def test_sync_from_code_handles_missing_files(tmp_path: Path) -> None:
    """Sync does not crash when a referenced model file is missing."""
    # Schema references a file that doesn't exist on disk
    table = _users_table(file_path="app/models.py")
    alter_file, staging = _make_schema(tmp_path, [table])

    # Should not raise — returns a summary (possibly with skipped note)
    result = _sync_from_code_impl(staging, tmp_path, alter_file=alter_file)
    assert isinstance(result, str)


_SQLMODEL_USER_WITH_NAME = """\
from __future__ import annotations
import uuid
from sqlmodel import SQLModel, Field

class Users(SQLModel, table=True):
    __tablename__ = "users"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    email: str = Field(unique=True)
    name: str = Field(unique=False)
"""


def test_apply_then_sync_round_trip(tmp_path: Path) -> None:
    """apply writes models.py; a hand-edited file with a new column syncs back into the schema."""
    alter_file, staging = _make_schema(tmp_path, [_users_table()])

    # Write initial model file from schema (id + email)
    _apply_to_code_impl(staging, tmp_path, preview=False)

    # Developer adds a "name" column by hand to models.py
    models_file = tmp_path / "app" / "models.py"
    models_file.write_text(_SQLMODEL_USER_WITH_NAME)

    # Sync should pick up the new column
    _sync_from_code_impl(staging, tmp_path, alter_file=alter_file)

    users_table = next(t for t in staging.current_schema.tables if t.name == "users")
    col_names = {c.name for c in users_table.columns}
    assert "name" in col_names


_SQLMODEL_WITH_ENUM = """\
from enum import Enum
from sqlmodel import SQLModel, Field

class Role(str, Enum):
    admin = "admin"
    member = "member"

class Memberships(SQLModel, table=True):
    __tablename__ = "memberships"
    id: int = Field(primary_key=True)
    role: Role
"""


def test_sync_preserves_enum_definitions(tmp_path: Path) -> None:
    """Regression: sync must include enum definitions so validation does not fail.

    A model file that defines a custom Enum and uses it as a column type must
    produce a schema where the enum is registered, otherwise AlterSchema
    validation raises 'unknown type <EnumName>'.
    """
    models_file = tmp_path / "app" / "models.py"
    models_file.parent.mkdir(parents=True)
    models_file.write_text(_SQLMODEL_WITH_ENUM)

    from alter.schema import EnumDef

    memberships_table = Table(
        name="memberships",
        file_path="app/models.py",
        columns=[
            Column(name="id", type="int", primary_key=True, nullable=False),
            Column(name="role", type="Role", nullable=True),
        ],
    )
    role_enum = EnumDef(name="Role", values=["admin", "member"])
    schema = AlterSchema(tables=[memberships_table], enums=[role_enum])
    alter_file = tmp_path / "schema.alter"
    schema.save(alter_file)
    staging = StagingManager(alter_file)

    # Must not raise a validation error
    _sync_from_code_impl(staging, tmp_path, alter_file=alter_file)

    enum_names = {e.name for e in staging.current_schema.enums}
    assert "Role" in enum_names
    # Schema on disk must also be loadable without validation errors
    loaded = AlterSchema.load(alter_file)
    assert any(e.name == "Role" for e in loaded.enums)


# ---------------------------------------------------------------------------
# Regression: _sync_from_code_impl must collect relations and deduplicate enums
# ---------------------------------------------------------------------------

_SQLMODEL_WITH_FK = """\
from __future__ import annotations
import uuid
from typing import Optional
from sqlmodel import SQLModel, Field

class Users(SQLModel, table=True):
    __tablename__ = "users"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    email: str = Field(unique=True)

class Posts(SQLModel, table=True):
    __tablename__ = "posts"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id")
    title: str = Field(max_length=200)
"""

_ENUM_FILE = """\
from enum import Enum

class Status(str, Enum):
    active = "active"
    inactive = "inactive"
"""

_SQLMODEL_WITH_IMPORTED_ENUM = """\
from __future__ import annotations
import uuid
from sqlmodel import SQLModel, Field
from app.enums import Status

class Items(SQLModel, table=True):
    __tablename__ = "items"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    status: Status = Field(default=Status.active)
"""


def test_sync_collects_relations_from_model_files(tmp_path: Path) -> None:
    """Regression: _sync_from_code_impl must populate schema.relations from FK columns.

    Previously, when parsing individual files, relations were silently dropped
    because only tables and enums were collected from parse_file_result().
    """
    models_file = tmp_path / "app" / "models.py"
    models_file.parent.mkdir(parents=True)
    models_file.write_text(_SQLMODEL_WITH_FK)

    users_table = Table(
        name="users",
        file_path="app/models.py",
        columns=[
            Column(name="id", type="uuid", primary_key=True),
            Column(name="email", type="string", unique=True),
        ],
    )
    posts_table = Table(
        name="posts",
        file_path="app/models.py",
        columns=[
            Column(name="id", type="uuid", primary_key=True),
            Column(name="user_id", type="uuid", foreign_key="users.id"),
            Column(name="title", type="string"),
        ],
    )
    alter_file, staging = _make_schema(tmp_path, [users_table, posts_table])

    _sync_from_code_impl(staging, tmp_path, alter_file=alter_file)

    relations = staging.current_schema.relations
    assert len(relations) >= 1, "Expected at least one relation from FK column"
    rel = next((r for r in relations if r.from_table == "posts" and r.from_column == "user_id"), None)
    assert rel is not None, "Expected posts.user_id -> users.id relation"
    assert rel.to_table == "users"
    assert rel.to_column == "id"

    # Persisted file must also have the relations
    loaded = AlterSchema.load(alter_file)
    assert any(r.from_table == "posts" for r in loaded.relations)


def test_sync_deduplicates_enums_across_files(tmp_path: Path) -> None:
    """Regression: syncing multiple files must not produce duplicate enum entries.

    When file_paths contains both an enum-only file (app/enums.py) AND a model
    file that imports those enums (app/models.py), parse_file_result() on the
    model file transitively includes the enums from app/enums.py.  Without
    deduplication, the same enum appears twice in the schema.
    """
    (tmp_path / "app").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "enums.py").write_text(_ENUM_FILE)
    (tmp_path / "app" / "models.py").write_text(_SQLMODEL_WITH_IMPORTED_ENUM)

    from alter.schema import EnumDef

    items_table = Table(
        name="items",
        file_path="app/models.py",
        columns=[
            Column(name="id", type="uuid", primary_key=True),
            Column(name="status", type="Status"),
        ],
    )
    status_enum = EnumDef(name="Status", values=["active", "inactive"], file_path="app/enums.py")
    schema = AlterSchema(orm="sqlmodel", tables=[items_table], enums=[status_enum])
    alter_file = tmp_path / "schema.alter"
    schema.save(alter_file)
    staging = StagingManager(alter_file)

    _sync_from_code_impl(staging, tmp_path, alter_file=alter_file)

    enum_names = [e.name for e in staging.current_schema.enums]
    # "Status" must appear exactly once
    assert enum_names.count("Status") == 1, (
        f"Expected Status to appear once, got {enum_names}"
    )

