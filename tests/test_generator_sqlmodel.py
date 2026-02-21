"""Tests for the SQLModel generator backend.

Uses inline schemas so there are no external file dependencies.
"""

from __future__ import annotations

import ast
import tempfile
import os
from pathlib import Path
from textwrap import dedent

import pytest

from alter.generators.sqlmodel import SQLModelGenerator
from alter.generators.base import get_generator
from alter.schema import AlterSchema, Column, EnumDef, Table


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def simple_schema(**kwargs) -> AlterSchema:
    """Minimal AlterSchema with one table."""
    defaults = dict(
        orm="sqlmodel",
        tables=[
            Table(
                name="items",
                file_path="app/models.py",
                columns=[
                    Column(name="id", type="uuid", primary_key=True, nullable=False,
                           default="uuid4"),
                    Column(name="name", type="string", nullable=False, max_length=100),
                ],
            )
        ],
    )
    defaults.update(kwargs)
    return AlterSchema(**defaults)


def gen() -> SQLModelGenerator:
    return SQLModelGenerator()


def parse_generated(code: str) -> ast.Module:
    """Assert code is syntactically valid Python and return its AST."""
    return ast.parse(code)


# ---------------------------------------------------------------------------
# 1. get_generator factory
# ---------------------------------------------------------------------------

def test_get_generator_returns_sqlmodel():
    g = get_generator("sqlmodel")
    assert isinstance(g, SQLModelGenerator)


def test_get_generator_raises_on_unknown():
    with pytest.raises(ValueError, match="Unknown ORM"):
        get_generator("django")


# ---------------------------------------------------------------------------
# 2. generate_models() — imports
# ---------------------------------------------------------------------------

def test_generate_imports_uuid():
    schema = simple_schema()
    code = gen().generate_models(schema)
    assert "import uuid" in code


def test_generate_imports_optional_when_nullable():
    schema = AlterSchema(
        orm="sqlmodel",
        tables=[Table(name="t", columns=[
            Column(name="x", type="string", nullable=True),
        ])],
    )
    code = gen().generate_models(schema)
    assert "from typing import Optional" in code


def test_generate_no_optional_import_when_not_needed():
    schema = simple_schema()  # no nullable columns
    code = gen().generate_models(schema)
    assert "from typing import Optional" not in code


def test_generate_imports_datetime():
    schema = AlterSchema(
        orm="sqlmodel",
        tables=[Table(name="t", columns=[
            Column(name="created_at", type="datetime", nullable=False,
                   default="utcnow"),
        ])],
    )
    code = gen().generate_models(schema)
    assert "from datetime import datetime" in code


def test_generate_imports_enum_when_enums_present():
    schema = AlterSchema(
        orm="sqlmodel",
        enums=[EnumDef(name="Status", values=["active", "inactive"])],
        tables=[Table(name="t", columns=[
            Column(name="status", type="Status", nullable=False),
        ])],
    )
    code = gen().generate_models(schema)
    assert "from enum import Enum" in code


# ---------------------------------------------------------------------------
# 3. generate_models() — class structure
# ---------------------------------------------------------------------------

def test_generate_valid_python():
    schema = simple_schema()
    code = gen().generate_models(schema)
    parse_generated(code)  # raises if invalid


def test_generate_tablename_explicit():
    schema = simple_schema()
    code = gen().generate_models(schema)
    assert '__tablename__ = "items"' in code


def test_generate_primary_key_field():
    schema = simple_schema()
    code = gen().generate_models(schema)
    assert "primary_key=True" in code


def test_generate_default_factory_uuid():
    schema = simple_schema()
    code = gen().generate_models(schema)
    assert "default_factory=uuid.uuid4" in code


def test_generate_default_factory_utcnow():
    schema = AlterSchema(
        orm="sqlmodel",
        tables=[Table(name="t", columns=[
            Column(name="created_at", type="datetime", nullable=False, default="utcnow"),
        ])],
    )
    code = gen().generate_models(schema)
    assert "default_factory=datetime.utcnow" in code


def test_generate_unique_and_index():
    schema = AlterSchema(
        orm="sqlmodel",
        tables=[Table(name="t", columns=[
            Column(name="email", type="string", nullable=False,
                   unique=True, index=True, max_length=255),
        ])],
    )
    code = gen().generate_models(schema)
    assert "unique=True" in code
    assert "index=True" in code
    assert "max_length=255" in code


def test_generate_foreign_key():
    schema = AlterSchema(
        orm="sqlmodel",
        tables=[Table(name="t", columns=[
            Column(name="user_id", type="uuid", nullable=False,
                   foreign_key="users.id"),
        ])],
    )
    code = gen().generate_models(schema)
    assert 'foreign_key="users.id"' in code


def test_generate_optional_nullable_column():
    schema = AlterSchema(
        orm="sqlmodel",
        tables=[Table(name="t", columns=[
            Column(name="bio", type="string", nullable=True),
        ])],
    )
    code = gen().generate_models(schema)
    assert "Optional[str]" in code
    assert "default=None" in code


def test_generate_enum_class_before_model():
    schema = AlterSchema(
        orm="sqlmodel",
        enums=[EnumDef(name="Role", values=["admin", "member"])],
        tables=[Table(name="t", columns=[
            Column(name="role", type="Role", nullable=False, default="member"),
        ])],
    )
    code = gen().generate_models(schema)
    assert "class Role(str, Enum):" in code
    # enum class appears before model class
    assert code.index("class Role") < code.index("class T(SQLModel")


def test_generate_enum_default_uses_enum_class():
    schema = AlterSchema(
        orm="sqlmodel",
        enums=[EnumDef(name="Status", values=["active", "inactive"])],
        tables=[Table(name="t", columns=[
            Column(name="status", type="Status", nullable=False, default="active"),
        ])],
    )
    code = gen().generate_models(schema)
    assert "default=Status.active" in code


def test_generate_bool_defaults():
    schema = AlterSchema(
        orm="sqlmodel",
        tables=[Table(name="t", columns=[
            Column(name="flag", type="bool", nullable=False, default="false"),
        ])],
    )
    code = gen().generate_models(schema)
    assert "default=False" in code


def test_generate_multiple_tables():
    schema = AlterSchema(
        orm="sqlmodel",
        tables=[
            Table(name="users", columns=[Column(name="id", type="uuid",
                  primary_key=True, nullable=False, default="uuid4")]),
            Table(name="posts", columns=[Column(name="id", type="uuid",
                  primary_key=True, nullable=False, default="uuid4")]),
        ],
    )
    code = gen().generate_models(schema)
    assert "class Users(SQLModel" in code
    assert "class Posts(SQLModel" in code


# ---------------------------------------------------------------------------
# 4. update_models() — surgical update
# ---------------------------------------------------------------------------

EXISTING_FILE = dedent("""\
    # This is a top-level comment that must be preserved.

    import uuid
    from typing import Optional
    from sqlmodel import Field, SQLModel


    def helper():
        \"\"\"A helper function that must not be touched.\"\"\"
        return 42


    class Item(SQLModel, table=True):
        __tablename__ = "items"

        id: uuid.UUID = Field(primary_key=True, default_factory=uuid.uuid4)
        name: str = Field(max_length=100)


    # trailing comment
""")


def test_update_preserves_comments():
    schema = simple_schema()
    schema.tables[0].name = "items"
    # add a column to trigger a change
    schema.tables[0].columns.append(
        Column(name="price", type="int", nullable=False)
    )
    result = gen().update_models(schema, EXISTING_FILE)
    assert "# This is a top-level comment that must be preserved." in result
    assert "# trailing comment" in result


def test_update_preserves_helper_function():
    schema = simple_schema()
    schema.tables[0].name = "items"
    schema.tables[0].columns.append(Column(name="price", type="int", nullable=False))
    result = gen().update_models(schema, EXISTING_FILE)
    assert "def helper():" in result
    assert "return 42" in result


def test_update_modifies_changed_class():
    schema = simple_schema()
    schema.tables[0].name = "items"
    schema.tables[0].columns.append(Column(name="price", type="int", nullable=False))
    result = gen().update_models(schema, EXISTING_FILE)
    assert "price" in result


def test_update_appends_new_class():
    """A new table not in the existing file is appended at the bottom."""
    schema = AlterSchema(
        orm="sqlmodel",
        tables=[
            Table(name="items", columns=[
                Column(name="id", type="uuid", primary_key=True, nullable=False,
                       default="uuid4"),
            ]),
            Table(name="tags", columns=[
                Column(name="id", type="uuid", primary_key=True, nullable=False,
                       default="uuid4"),
                Column(name="label", type="string", nullable=False, max_length=50),
            ]),
        ],
    )
    result = gen().update_models(schema, EXISTING_FILE)
    assert "class Tags(SQLModel" in result
    # Tags must appear after Item
    assert result.index("class Tags") > result.index("class Item")


def test_update_unchanged_class_not_modified():
    """If the class matches schema exactly, the source lines are untouched."""
    # Build schema that exactly matches EXISTING_FILE's Item class
    schema = AlterSchema(
        orm="sqlmodel",
        tables=[Table(name="items", columns=[
            Column(name="id", type="uuid", primary_key=True, nullable=False,
                   default="uuid4"),
            Column(name="name", type="string", nullable=False, max_length=100),
        ])],
    )
    result = gen().update_models(schema, EXISTING_FILE)
    # The result should still contain the helper function
    assert "def helper():" in result


def test_update_adds_missing_imports():
    """Surgical update adds missing imports when new types are introduced (spec §1C.2 case 3)."""
    # File with only basic imports — no datetime, no Optional
    minimal_file = dedent("""\
        import uuid
        from sqlmodel import Field, SQLModel


        class Item(SQLModel, table=True):
            __tablename__ = "items"

            id: uuid.UUID = Field(primary_key=True, default_factory=uuid.uuid4)
            name: str = Field(max_length=100)
    """)
    # Schema adds a nullable datetime column — needs Optional + datetime imports
    schema = AlterSchema(
        orm="sqlmodel",
        tables=[Table(name="items", columns=[
            Column(name="id", type="uuid", primary_key=True, nullable=False,
                   default="uuid4"),
            Column(name="name", type="string", nullable=False, max_length=100),
            Column(name="expires_at", type="datetime", nullable=True),  # new
        ])],
    )
    result = gen().update_models(schema, minimal_file)
    # The result must import both Optional and datetime
    assert "from typing import Optional" in result
    assert "from datetime import datetime" in result
    # And the result must be valid Python
    ast.parse(result)


def test_update_syntax_error_falls_back_to_generate():
    bad_code = "class (\n    broken\n"
    schema = simple_schema()
    result = gen().update_models(schema, bad_code)
    # Should still produce valid code via fallback
    ast.parse(result)


# ---------------------------------------------------------------------------
# 5. preview_apply()
# ---------------------------------------------------------------------------

def test_preview_apply_returns_diff_string(tmp_path: Path):
    # Write an existing file to tmp_path
    models_dir = tmp_path / "app"
    models_dir.mkdir()
    (models_dir / "models.py").write_text(EXISTING_FILE)

    schema = AlterSchema(
        orm="sqlmodel",
        tables=[Table(name="items", file_path="app/models.py", columns=[
            Column(name="id", type="uuid", primary_key=True, nullable=False,
                   default="uuid4"),
            Column(name="name", type="string", nullable=False, max_length=100),
            Column(name="price", type="int", nullable=False),  # new column
        ])],
    )
    diff = gen().preview_apply(schema, tmp_path)
    assert "---" in diff
    assert "+++" in diff
    assert "price" in diff


def test_preview_apply_writes_no_files(tmp_path: Path):
    models_dir = tmp_path / "app"
    models_dir.mkdir()
    original = EXISTING_FILE
    (models_dir / "models.py").write_text(original)

    schema = AlterSchema(
        orm="sqlmodel",
        tables=[Table(name="items", file_path="app/models.py", columns=[
            Column(name="id", type="uuid", primary_key=True, nullable=False,
                   default="uuid4"),
            Column(name="extra", type="string", nullable=False),
        ])],
    )
    gen().preview_apply(schema, tmp_path)
    # File on disk must be unchanged
    assert (models_dir / "models.py").read_text() == original


def test_preview_apply_empty_when_no_changes(tmp_path: Path):
    """No diff returned when generated code matches file on disk."""
    schema = AlterSchema(
        orm="sqlmodel",
        tables=[Table(name="items", file_path="app/models.py", columns=[
            Column(name="id", type="uuid", primary_key=True, nullable=False,
                   default="uuid4"),
            Column(name="name", type="string", nullable=False, max_length=100),
        ])],
    )
    # Write what the generator would produce
    models_dir = tmp_path / "app"
    models_dir.mkdir()
    (models_dir / "models.py").write_text(gen().generate_models(schema))

    diff = gen().preview_apply(schema, tmp_path)
    assert diff == ""


# ---------------------------------------------------------------------------
# 6. Multi-file and default file_path
# ---------------------------------------------------------------------------

def test_default_filepath_is_app_models(tmp_path: Path):
    """Table without file_path uses app/models.py."""
    schema = AlterSchema(
        orm="sqlmodel",
        tables=[Table(name="things", columns=[
            Column(name="id", type="int", primary_key=True, nullable=False),
        ])],
        # file_path NOT set on table
    )
    (tmp_path / "app").mkdir()
    diff = gen().preview_apply(schema, tmp_path)
    # diff fromfile should reference app/models.py
    assert "app/models.py" in diff


def test_multifile_preview_touches_correct_files(tmp_path: Path):
    """Tables in different file_paths produce separate diff sections."""
    (tmp_path / "app").mkdir()
    schema = AlterSchema(
        orm="sqlmodel",
        tables=[
            Table(name="users", file_path="app/users.py", columns=[
                Column(name="id", type="uuid", primary_key=True, nullable=False,
                       default="uuid4"),
            ]),
            Table(name="posts", file_path="app/posts.py", columns=[
                Column(name="id", type="uuid", primary_key=True, nullable=False,
                       default="uuid4"),
            ]),
        ],
    )
    diff = gen().preview_apply(schema, tmp_path)
    assert "app/users.py" in diff
    assert "app/posts.py" in diff


# ---------------------------------------------------------------------------
# 7. Surgical update — preserve docstrings, Relationships, comments
# ---------------------------------------------------------------------------

EXISTING_WITH_RELATIONSHIPS = dedent("""\
    import uuid
    from typing import Optional
    from sqlmodel import Field, Relationship, SQLModel


    class User(SQLModel, table=True):
        \"\"\"Application user account.\"\"\"
        __tablename__ = "users"

        id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
        name: str = Field(max_length=100)

        memberships: list["Membership"] = Relationship(back_populates="user")
        # Nullable relation — AuditLog.user_id is Optional
        audit_logs: list["AuditLog"] = Relationship(back_populates="user")
""")


def _users_schema(**col_overrides) -> AlterSchema:
    """Build a schema with a single 'users' table."""
    cols = [
        Column(name="id", type="uuid", primary_key=True, nullable=False, default="uuid4"),
        Column(name="name", type="string", nullable=False, max_length=100),
    ]
    cols_dict = {c.name: c for c in cols}
    cols_dict.update(col_overrides)
    return AlterSchema(
        orm="sqlmodel",
        tables=[Table(name="users", columns=list(cols_dict.values()))],
    )


def test_update_preserves_docstring_when_schema_unchanged():
    schema = _users_schema()
    result = gen().update_models(schema, EXISTING_WITH_RELATIONSHIPS)
    assert '"""Application user account."""' in result


def test_update_preserves_relationship_lines_when_schema_unchanged():
    schema = _users_schema()
    result = gen().update_models(schema, EXISTING_WITH_RELATIONSHIPS)
    assert 'Relationship(back_populates="user")' in result
    assert "memberships" in result
    assert "audit_logs" in result


def test_update_preserves_inline_comment_when_schema_unchanged():
    schema = _users_schema()
    result = gen().update_models(schema, EXISTING_WITH_RELATIONSHIPS)
    assert "# Nullable relation" in result


def test_update_new_column_inserted_before_relationship():
    email_col = Column(name="email", type="string", nullable=False, max_length=255, unique=True, index=True)
    schema = AlterSchema(
        orm="sqlmodel",
        tables=[Table(name="users", columns=[
            Column(name="id", type="uuid", primary_key=True, nullable=False, default="uuid4"),
            Column(name="name", type="string", nullable=False, max_length=100),
            email_col,
        ])],
    )
    result = gen().update_models(schema, EXISTING_WITH_RELATIONSHIPS)
    assert "email" in result
    assert result.index("email") < result.index("Relationship(")


def test_update_new_table_does_not_touch_existing_class():
    """Adding a second table must not modify the existing User class."""
    schema = AlterSchema(
        orm="sqlmodel",
        tables=[
            Table(name="users", columns=[
                Column(name="id", type="uuid", primary_key=True, nullable=False, default="uuid4"),
                Column(name="name", type="string", nullable=False, max_length=100),
            ]),
            Table(name="teams", columns=[
                Column(name="id", type="uuid", primary_key=True, nullable=False, default="uuid4"),
            ]),
        ],
    )
    result = gen().update_models(schema, EXISTING_WITH_RELATIONSHIPS)
    # Original User class content is intact
    assert '"""Application user account."""' in result
    assert 'Relationship(back_populates="user")' in result
    assert "# Nullable relation" in result
    # New table was appended
    assert "class Teams(SQLModel" in result


def test_update_changed_column_updates_only_that_field_line():
    schema = _users_schema(
        name=Column(name="name", type="string", nullable=False, max_length=200)
    )
    result = gen().update_models(schema, EXISTING_WITH_RELATIONSHIPS)
    assert "max_length=200" in result
    assert "max_length=100" not in result
    # Non-schema content preserved
    assert '"""Application user account."""' in result
    assert 'Relationship(back_populates="user")' in result


def test_update_kwarg_order_preserved_for_unchanged_field():
    """Unchanged id field must keep its hand-written kwarg order."""
    # Schema has canonical order (pk first); file has default_factory first
    schema = _users_schema(
        name=Column(name="name", type="string", nullable=False, max_length=200)  # changed
    )
    result = gen().update_models(schema, EXISTING_WITH_RELATIONSHIPS)
    # id field: hand-written order 'default_factory, primary_key' preserved
    assert "Field(default_factory=uuid.uuid4, primary_key=True)" in result


# ---------------------------------------------------------------------------
# 14. local_enum_names — cross-file enum emission control
# ---------------------------------------------------------------------------

def test_update_models_does_not_append_imported_enum():
    """Enums imported from another file must NOT be appended as class definitions."""
    existing = dedent("""\
        import uuid
        from app.enums import Role

        class User(SQLModel, table=True):
            __tablename__ = "users"

            id: uuid.UUID = Field(primary_key=True, default_factory=uuid.uuid4)
            role: Role = Field(default=Role.member)
    """)

    schema = AlterSchema(
        orm="sqlmodel",
        tables=[
            Table(
                name="users",
                file_path="app/models/users.py",
                columns=[
                    Column(name="id", type="uuid", primary_key=True, nullable=False,
                           default="uuid4"),
                    Column(name="role", type="Role", nullable=False, default="member"),
                ],
            )
        ],
        enums=[
            # Role is defined in app/enums.py — NOT in app/models/users.py
            EnumDef(name="Role", values=["admin", "member", "viewer"],
                    file_path="app/enums.py"),
        ],
    )
    # Pass local_enum_names=empty set — this file defines no enums
    local_enum_names: set[str] = set()
    result = gen().update_models(schema, existing, local_enum_names=local_enum_names)

    assert "class Role" not in result, "Role class must not be appended to model file"
    # Original import must be preserved
    assert "from app.enums import Role" in result
    # Type annotation must still use Role (not str)
    assert "role: Role" in result or "Optional[Role]" in result


def test_update_models_type_resolution_uses_all_enum_names():
    """Column typed with an external enum must still produce correct type hint."""
    existing = dedent("""\
        import uuid
        from app.enums import Status

        class Item(SQLModel, table=True):
            __tablename__ = "items"

            id: uuid.UUID = Field(primary_key=True, default_factory=uuid.uuid4)
    """)

    schema = AlterSchema(
        orm="sqlmodel",
        tables=[
            Table(
                name="items",
                file_path="app/models/items.py",
                columns=[
                    Column(name="id", type="uuid", primary_key=True, nullable=False,
                           default="uuid4"),
                    Column(name="status", type="Status", nullable=True),  # new column
                ],
            )
        ],
        enums=[
            EnumDef(name="Status", values=["active", "inactive"],
                    file_path="app/enums.py"),
        ],
    )
    local_enum_names: set[str] = set()
    result = gen().update_models(schema, existing, local_enum_names=local_enum_names)

    # New column added with correct enum type hint (not 'str')
    assert "status: Optional[Status]" in result
    # No class definition for Status in this file
    assert "class Status" not in result


def test_generate_models_does_not_emit_foreign_enums():
    """generate_models() only emits enum classes for local_enum_names."""
    schema = AlterSchema(
        orm="sqlmodel",
        tables=[
            Table(
                name="items",
                file_path="app/models.py",
                columns=[
                    Column(name="id", type="uuid", primary_key=True, nullable=False,
                           default="uuid4"),
                    Column(name="status", type="Status", nullable=False, default="active"),
                ],
            )
        ],
        enums=[
            EnumDef(name="Status", values=["active", "inactive"],
                    file_path="app/enums.py"),
        ],
    )
    # No local enums — Status is defined in app/enums.py
    result = gen().generate_models(schema, local_enum_names=set())

    assert "class Status" not in result
    # from enum import Enum must not be added since no local enum classes
    assert "from enum import Enum" not in result
    # Type resolution must still use Status (not str)
    assert "status: Status" in result


def test_update_models_inherited_columns_not_added_to_class_body():
    """Columns marked inherited=True must not be inserted into the class body."""
    existing = dedent("""\
        import uuid
        from datetime import datetime
        from app.base import UUIDBase, TimestampedBase

        class Post(UUIDBase, TimestampedBase, table=True):
            __tablename__ = "posts"

            title: str = Field(max_length=500)
    """)

    schema = AlterSchema(
        orm="sqlmodel",
        tables=[
            Table(
                name="posts",
                file_path="app/models/posts.py",
                bases=["UUIDBase", "TimestampedBase"],
                columns=[
                    # inherited columns from UUIDBase / TimestampedBase
                    Column(name="id", type="uuid", primary_key=True, nullable=False,
                           default="uuid4", inherited=True),
                    Column(name="created_at", type="datetime", nullable=False,
                           default="utcnow", inherited=True),
                    # local column
                    Column(name="title", type="string", nullable=False, max_length=500),
                ],
            )
        ],
    )
    result = gen().update_models(schema, existing)

    # Inherited columns must NOT be inserted as explicit field definitions
    assert result.count("id: uuid.UUID") == 0, "id from UUIDBase must not be added"
    assert result.count("created_at: datetime") == 0, "created_at must not be added"
    # Local column preserved
    assert "title: str = Field(max_length=500)" in result
    # Class unchanged overall
    assert "class Post(UUIDBase, TimestampedBase, table=True):" in result


