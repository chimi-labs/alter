"""Tests for the canvas /api/apply-to-code and /api/sync-from-code endpoints.

Tests call _apply_to_code_impl and _sync_from_code_impl directly, bypassing
HTTP transport, following the same pattern used in test_mcp_server.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alter.mcp_server import _apply_to_code_impl, _sync_from_code_impl
from alter.canvas.server import _apply_modify_column, _MODIFIABLE_COL_FIELDS
from alter.schema import AlterSchema, Column, EnumDef, Position, Relation, Table
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


# ---------------------------------------------------------------------------
# _apply_modify_column — whitelist / validation tests
# ---------------------------------------------------------------------------


def _make_simple_schema() -> AlterSchema:
    """Two tables (users, orders) with a FK relation for rename tests."""
    users = Table(
        name="users",
        columns=[
            Column(name="id", type="uuid", primary_key=True, nullable=False),
            Column(name="email", type="string", nullable=False, unique=True),
            Column(name="bio", type="text", nullable=True),
        ],
    )
    orders = Table(
        name="orders",
        columns=[
            Column(name="id", type="uuid", primary_key=True, nullable=False),
            Column(name="user_id", type="uuid", foreign_key="users.id"),
        ],
    )
    rel = Relation(
        name="orders_user_id_fk",
        from_table="orders",
        from_column="user_id",
        to_table="users",
        to_column="id",
    )
    return AlterSchema(orm="sqlmodel", tables=[users, orders], relations=[rel])


class TestApplyModifyColumnWhitelist:
    """Only whitelisted fields should be mutated."""

    def test_whitelisted_field_nullable_is_applied(self) -> None:
        s = _make_simple_schema()
        col = next(c for t in s.tables if t.name == "users" for c in t.columns if c.name == "bio")
        assert col.nullable is True
        _apply_modify_column(s, "users", "bio", {"nullable": False})
        assert col.nullable is False

    def test_whitelisted_field_unique_is_applied(self) -> None:
        s = _make_simple_schema()
        col = next(c for t in s.tables if t.name == "users" for c in t.columns if c.name == "bio")
        _apply_modify_column(s, "users", "bio", {"unique": True})
        assert col.unique is True

    def test_non_whitelisted_field_is_silently_ignored(self) -> None:
        s = _make_simple_schema()
        col = next(c for t in s.tables if t.name == "users" for c in t.columns if c.name == "email")
        # 'primary_key' is not in _MODIFIABLE_COL_FIELDS
        original_pk = col.primary_key
        _apply_modify_column(s, "users", "email", {"primary_key": True})
        assert col.primary_key == original_pk

    def test_unknown_field_is_silently_ignored(self) -> None:
        s = _make_simple_schema()
        col = next(c for t in s.tables if t.name == "users" for c in t.columns if c.name == "email")
        # Arbitrary payload key should not raise and should not set attr
        _apply_modify_column(s, "users", "email", {"__class__": "hacked"})
        assert col.__class__ is Column

    def test_whitelist_constant_contents(self) -> None:
        expected = {"name", "type", "nullable", "unique", "default", "max_length", "index", "foreign_key"}
        assert _MODIFIABLE_COL_FIELDS == expected


class TestApplyModifyColumnTypeValidation:
    """type updates must be a known built-in or declared enum."""

    def test_valid_builtin_type_is_applied(self) -> None:
        s = _make_simple_schema()
        col = next(c for t in s.tables if t.name == "users" for c in t.columns if c.name == "bio")
        _apply_modify_column(s, "users", "bio", {"type": "int"})
        assert col.type == "int"

    def test_invalid_type_is_rejected(self) -> None:
        s = _make_simple_schema()
        col = next(c for t in s.tables if t.name == "users" for c in t.columns if c.name == "bio")
        original_type = col.type
        _apply_modify_column(s, "users", "bio", {"type": "NOT_A_REAL_TYPE"})
        assert col.type == original_type

    def test_declared_enum_type_is_accepted(self) -> None:
        s = _make_simple_schema()
        s.enums.append(EnumDef(name="Status", values=["active", "inactive"]))
        col = next(c for t in s.tables if t.name == "users" for c in t.columns if c.name == "bio")
        _apply_modify_column(s, "users", "bio", {"type": "Status"})
        assert col.type == "Status"

    def test_undeclared_enum_name_is_rejected(self) -> None:
        s = _make_simple_schema()
        col = next(c for t in s.tables if t.name == "users" for c in t.columns if c.name == "bio")
        original_type = col.type
        # "Status" is not in s.enums, so it must be rejected
        _apply_modify_column(s, "users", "bio", {"type": "Status"})
        assert col.type == original_type


class TestApplyModifyColumnRename:
    """name updates should cascade to relations and FK refs."""

    def test_rename_updates_column_name(self) -> None:
        s = _make_simple_schema()
        _apply_modify_column(s, "users", "email", {"name": "email_address"})
        names = [c.name for t in s.tables if t.name == "users" for c in t.columns]
        assert "email_address" in names
        assert "email" not in names

    def test_rename_updates_relation_from_column(self) -> None:
        s = _make_simple_schema()
        _apply_modify_column(s, "orders", "user_id", {"name": "owner_id"})
        rel = s.relations[0]
        assert rel.from_column == "owner_id"

    def test_rename_updates_foreign_key_refs(self) -> None:
        s = _make_simple_schema()
        # Rename the target column users.id → users.uid
        _apply_modify_column(s, "users", "id", {"name": "uid"})
        fk_col = next(c for t in s.tables if t.name == "orders" for c in t.columns if c.name == "user_id")
        assert fk_col.foreign_key == "users.uid"

    def test_rename_to_duplicate_is_rejected(self) -> None:
        s = _make_simple_schema()
        # "email" already exists in users; renaming "bio" → "email" should be a no-op
        _apply_modify_column(s, "users", "bio", {"name": "email"})
        names = [c.name for t in s.tables if t.name == "users" for c in t.columns]
        assert names.count("email") == 1
        assert "bio" in names

    def test_rename_to_empty_string_is_rejected(self) -> None:
        s = _make_simple_schema()
        _apply_modify_column(s, "users", "bio", {"name": ""})
        names = [c.name for t in s.tables if t.name == "users" for c in t.columns]
        assert "bio" in names

    def test_nonexistent_table_is_a_noop(self) -> None:
        s = _make_simple_schema()
        # Should not raise
        _apply_modify_column(s, "ghost", "bio", {"nullable": False})

    def test_nonexistent_column_is_a_noop(self) -> None:
        s = _make_simple_schema()
        _apply_modify_column(s, "users", "ghost_col", {"nullable": False})


# ---------------------------------------------------------------------------
# Bug 09 — deletion tests
# ---------------------------------------------------------------------------


def _users_table_full(file_path: str = "app/models.py") -> Table:
    """users table with id, email, role, created_at columns."""
    from alter.schema import Column
    return Table(
        name="users",
        file_path=file_path,
        columns=[
            Column(name="id", type="uuid", primary_key=True),
            Column(name="email", type="string", unique=True),
            Column(name="role", type="string"),
            Column(name="created_at", type="datetime"),
        ],
    )


def test_apply_removes_deleted_column(tmp_path: Path) -> None:
    """Deleting a column from the schema and applying must remove the field."""
    # Create table with 4 columns and write model file
    alter_file, staging = _make_schema(tmp_path, [_users_table_full()])
    _apply_to_code_impl(staging, tmp_path, preview=False)

    model_file = tmp_path / "app" / "models.py"
    assert "role" in model_file.read_text()

    # Now delete 'role' from the schema (simulate canvas deletion)
    import copy
    def drop_role(s: AlterSchema) -> AlterSchema:
        s2 = copy.deepcopy(s)
        for t in s2.tables:
            if t.name == "users":
                t.columns = [c for c in t.columns if c.name != "role"]
        return s2

    staging.propose(drop_role)
    staging.commit()

    msg = _apply_to_code_impl(staging, tmp_path, preview=False)
    assert "role" not in model_file.read_text()
    assert "email" in model_file.read_text()
    assert "id" in model_file.read_text()


def test_apply_delete_column_preview_shows_diff(tmp_path: Path) -> None:
    """Preview mode must show the deleted field as a removed line."""
    alter_file, staging = _make_schema(tmp_path, [_users_table_full()])
    _apply_to_code_impl(staging, tmp_path, preview=False)

    import copy
    def drop_role(s: AlterSchema) -> AlterSchema:
        s2 = copy.deepcopy(s)
        for t in s2.tables:
            if t.name == "users":
                t.columns = [c for c in t.columns if c.name != "role"]
        return s2

    staging.propose(drop_role)
    staging.commit()

    diff = _apply_to_code_impl(staging, tmp_path, preview=True)
    assert "-" in diff       # there are removed lines
    assert "role" in diff    # the removed line mentions 'role'


def test_apply_removes_deleted_table_class_from_shared_file(tmp_path: Path) -> None:
    """Deleting a table that shares a file with another table removes only its class."""
    from alter.schema import Column
    users = Table(
        name="users",
        file_path="app/models.py",
        columns=[Column(name="id", type="uuid", primary_key=True)],
    )
    posts = Table(
        name="posts",
        file_path="app/models.py",
        columns=[Column(name="id", type="uuid", primary_key=True)],
    )
    alter_file, staging = _make_schema(tmp_path, [users, posts])
    _apply_to_code_impl(staging, tmp_path, preview=False)

    model_file = tmp_path / "app" / "models.py"
    assert "Users" in model_file.read_text() or "users" in model_file.read_text()
    assert "Posts" in model_file.read_text() or "posts" in model_file.read_text()

    # Delete posts from schema
    import copy
    def drop_posts(s: AlterSchema) -> AlterSchema:
        s2 = copy.deepcopy(s)
        s2.tables = [t for t in s2.tables if t.name != "posts"]
        return s2

    staging.propose(drop_posts)
    staging.commit()

    _apply_to_code_impl(staging, tmp_path, preview=False)
    content = model_file.read_text()
    # users class must remain
    assert "__tablename__" in content
    assert '"users"' in content
    # posts class must be gone
    assert '"posts"' not in content


def test_apply_removes_deleted_table_class_from_own_file(tmp_path: Path) -> None:
    """Deleting the only table in a file must still update that file (Fix 4)."""
    from alter.schema import Column
    dummy = Table(
        name="dummy",
        file_path="app/dummy_models.py",
        columns=[Column(name="id", type="uuid", primary_key=True)],
    )
    main_table = Table(
        name="users",
        file_path="app/models.py",
        columns=[Column(name="id", type="uuid", primary_key=True)],
    )
    alter_file, staging = _make_schema(tmp_path, [main_table, dummy])
    _apply_to_code_impl(staging, tmp_path, preview=False)

    dummy_file = tmp_path / "app" / "dummy_models.py"
    assert dummy_file.exists()
    assert "Dummy" in dummy_file.read_text() or "dummy" in dummy_file.read_text()

    # Delete 'dummy' from schema — its file is no longer referenced by any table
    import copy
    def drop_dummy(s: AlterSchema) -> AlterSchema:
        s2 = copy.deepcopy(s)
        s2.tables = [t for t in s2.tables if t.name != "dummy"]
        return s2

    staging.propose(drop_dummy)
    staging.commit()

    # After apply, dummy_models.py must no longer contain the Dummy class
    _apply_to_code_impl(staging, tmp_path, preview=False)
    assert "Dummy" not in dummy_file.read_text()
    assert '"dummy"' not in dummy_file.read_text()


def test_apply_preserves_helper_function_after_column_deletion(tmp_path: Path) -> None:
    """Non-schema helpers (functions, comments) survive column deletion."""
    model_file = tmp_path / "app" / "models.py"
    model_file.parent.mkdir(parents=True, exist_ok=True)
    model_file.write_text(_SQLMODEL_WITH_HELPER)

    # Schema has only id + email (no extra columns) — matches helper file
    alter_file, staging = _make_schema(tmp_path, [_users_table()])

    # After apply no changes expected (file already matches schema)
    msg = _apply_to_code_impl(staging, tmp_path, preview=False)
    assert "get_display_name" in model_file.read_text()
