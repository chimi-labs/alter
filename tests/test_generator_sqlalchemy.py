"""Tests for the SQLAlchemy generator backend."""

from __future__ import annotations

import ast
from pathlib import Path
from textwrap import dedent

import pytest

from alter.generators.sqlalchemy import SQLAlchemyGenerator
from alter.generators.base import get_generator
from alter.schema import AlterSchema, Column, EnumDef, Table


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def simple_schema(**kwargs) -> AlterSchema:
    defaults = dict(
        orm="sqlalchemy",
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


def gen() -> SQLAlchemyGenerator:
    return SQLAlchemyGenerator()


def parse_ok(code: str) -> ast.Module:
    return ast.parse(code)


# ---------------------------------------------------------------------------
# 1. get_generator factory
# ---------------------------------------------------------------------------

def test_get_generator_returns_sqlalchemy():
    g = get_generator("sqlalchemy")
    assert isinstance(g, SQLAlchemyGenerator)


# ---------------------------------------------------------------------------
# 2. generate_models() — imports
# ---------------------------------------------------------------------------

def test_generate_imports_declarativebase():
    code = gen().generate_models(simple_schema())
    assert "DeclarativeBase" in code
    assert "from sqlalchemy.orm import" in code


def test_generate_imports_mapped():
    code = gen().generate_models(simple_schema())
    assert "Mapped" in code
    assert "mapped_column" in code


def test_generate_imports_foreignkey_when_needed():
    schema = AlterSchema(
        orm="sqlalchemy",
        tables=[Table(name="t", columns=[
            Column(name="user_id", type="uuid", nullable=False,
                   foreign_key="users.id"),
        ])],
    )
    code = gen().generate_models(schema)
    assert "ForeignKey" in code


def test_generate_imports_optional_when_nullable():
    schema = AlterSchema(
        orm="sqlalchemy",
        tables=[Table(name="t", columns=[
            Column(name="x", type="string", nullable=True),
        ])],
    )
    code = gen().generate_models(schema)
    assert "from typing import Optional" in code


def test_generate_includes_base_class():
    code = gen().generate_models(simple_schema())
    assert "class Base(DeclarativeBase):" in code


# ---------------------------------------------------------------------------
# 3. generate_models() — 2.0 style output
# ---------------------------------------------------------------------------

def test_generate_valid_python():
    code = gen().generate_models(simple_schema())
    parse_ok(code)


def test_generate_uses_mapped_annotation():
    code = gen().generate_models(simple_schema())
    assert "Mapped[" in code


def test_generate_uses_mapped_column():
    code = gen().generate_models(simple_schema())
    assert "mapped_column(" in code


def test_generate_tablename():
    code = gen().generate_models(simple_schema())
    assert '__tablename__ = "items"' in code


def test_generate_primary_key():
    code = gen().generate_models(simple_schema())
    assert "primary_key=True" in code


def test_generate_nullable_false_for_required():
    code = gen().generate_models(simple_schema())
    assert "nullable=False" in code


def test_generate_nullable_true_for_optional():
    schema = AlterSchema(
        orm="sqlalchemy",
        tables=[Table(name="t", columns=[
            Column(name="bio", type="string", nullable=True),
        ])],
    )
    code = gen().generate_models(schema)
    assert "nullable=True" in code
    assert "Optional[str]" in code


def test_generate_string_with_length():
    schema = AlterSchema(
        orm="sqlalchemy",
        tables=[Table(name="t", columns=[
            Column(name="title", type="string", nullable=False, max_length=255),
        ])],
    )
    code = gen().generate_models(schema)
    assert "String(255)" in code


def test_generate_foreign_key_expression():
    schema = AlterSchema(
        orm="sqlalchemy",
        tables=[Table(name="t", columns=[
            Column(name="user_id", type="uuid", nullable=False,
                   foreign_key="users.id"),
        ])],
    )
    code = gen().generate_models(schema)
    assert 'ForeignKey("users.id")' in code


def test_generate_unique_index():
    schema = AlterSchema(
        orm="sqlalchemy",
        tables=[Table(name="t", columns=[
            Column(name="email", type="string", nullable=False,
                   unique=True, index=True),
        ])],
    )
    code = gen().generate_models(schema)
    assert "unique=True" in code
    assert "index=True" in code


def test_generate_enum_class():
    schema = AlterSchema(
        orm="sqlalchemy",
        enums=[EnumDef(name="Role", values=["admin", "member"])],
        tables=[Table(name="t", columns=[
            Column(name="role", type="Role", nullable=False, default="member"),
        ])],
    )
    code = gen().generate_models(schema)
    assert "class Role(str, Enum):" in code
    assert 'admin = "admin"' in code


def test_generate_multiple_tables():
    schema = AlterSchema(
        orm="sqlalchemy",
        tables=[
            Table(name="users", columns=[Column(name="id", type="int",
                  primary_key=True, nullable=False)]),
            Table(name="posts", columns=[Column(name="id", type="int",
                  primary_key=True, nullable=False)]),
        ],
    )
    code = gen().generate_models(schema)
    assert "class Users(Base):" in code
    assert "class Posts(Base):" in code


# ---------------------------------------------------------------------------
# 4. update_models() — surgical update
# ---------------------------------------------------------------------------

EXISTING = dedent("""\
    # keep this comment

    import uuid
    from sqlalchemy import String, Uuid
    from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


    class Base(DeclarativeBase):
        pass


    def helper():
        return 99


    class Item(Base):
        __tablename__ = "items"

        id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, nullable=False)
        name: Mapped[str] = mapped_column(String(100), nullable=False)


    # end comment
""")


def test_sqla_update_preserves_comments():
    schema = simple_schema()
    schema.tables[0].name = "items"
    schema.tables[0].columns.append(Column(name="qty", type="int", nullable=False))
    result = gen().update_models(schema, EXISTING)
    assert "# keep this comment" in result
    assert "# end comment" in result


def test_sqla_update_preserves_helper():
    schema = simple_schema()
    schema.tables[0].name = "items"
    schema.tables[0].columns.append(Column(name="qty", type="int", nullable=False))
    result = gen().update_models(schema, EXISTING)
    assert "def helper():" in result
    assert "return 99" in result


def test_sqla_update_modifies_class():
    schema = simple_schema()
    schema.tables[0].name = "items"
    schema.tables[0].columns.append(Column(name="qty", type="int", nullable=False))
    result = gen().update_models(schema, EXISTING)
    assert "qty" in result


def test_sqla_update_appends_new_class():
    schema = AlterSchema(
        orm="sqlalchemy",
        tables=[
            Table(name="items", columns=[
                Column(name="id", type="uuid", primary_key=True, nullable=False,
                       default="uuid4"),
            ]),
            Table(name="orders", columns=[
                Column(name="id", type="uuid", primary_key=True, nullable=False,
                       default="uuid4"),
            ]),
        ],
    )
    result = gen().update_models(schema, EXISTING)
    assert "class Orders(Base):" in result
    assert result.index("class Orders") > result.index("class Item")


def test_sqla_update_adds_missing_imports():
    """Surgical update adds missing imports when new types are introduced (spec §1C.3 case 3)."""
    minimal_file = dedent("""\
        from sqlalchemy import Integer
        from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


        class Base(DeclarativeBase):
            pass


        class Item(Base):
            __tablename__ = "items"

            id: Mapped[int] = mapped_column(Integer, primary_key=True, nullable=False)
    """)
    # Add a nullable datetime column — needs Optional + datetime
    schema = AlterSchema(
        orm="sqlalchemy",
        tables=[Table(name="items", columns=[
            Column(name="id", type="int", primary_key=True, nullable=False),
            Column(name="archived_at", type="datetime", nullable=True),  # new
        ])],
    )
    result = gen().update_models(schema, minimal_file)
    assert "from typing import Optional" in result
    assert "from datetime import datetime" in result
    ast.parse(result)


def test_sqla_update_syntax_error_falls_back():
    bad = "class (\n  broken\n"
    schema = simple_schema()
    result = gen().update_models(schema, bad)
    ast.parse(result)  # must be valid


# ---------------------------------------------------------------------------
# 5. preview_apply()
# ---------------------------------------------------------------------------

def test_sqla_preview_returns_diff(tmp_path: Path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "models.py").write_text(EXISTING)
    schema = simple_schema()
    schema.tables[0].name = "items"
    schema.tables[0].columns.append(Column(name="qty", type="int", nullable=False))
    diff = gen().preview_apply(schema, tmp_path)
    assert "---" in diff
    assert "qty" in diff


def test_sqla_preview_writes_no_files(tmp_path: Path):
    (tmp_path / "app").mkdir()
    original = EXISTING
    (tmp_path / "app" / "models.py").write_text(original)
    schema = simple_schema()
    schema.tables[0].columns.append(Column(name="qty", type="int", nullable=False))
    gen().preview_apply(schema, tmp_path)
    assert (tmp_path / "app" / "models.py").read_text() == original


def test_sqla_preview_empty_when_no_changes(tmp_path: Path):
    schema = simple_schema()
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "models.py").write_text(gen().generate_models(schema))
    diff = gen().preview_apply(schema, tmp_path)
    assert diff == ""


def test_sqla_multifile_diff_mentions_both_files(tmp_path: Path):
    (tmp_path / "app").mkdir()
    schema = AlterSchema(
        orm="sqlalchemy",
        tables=[
            Table(name="users", file_path="app/users.py", columns=[
                Column(name="id", type="int", primary_key=True, nullable=False),
            ]),
            Table(name="posts", file_path="app/posts.py", columns=[
                Column(name="id", type="int", primary_key=True, nullable=False),
            ]),
        ],
    )
    diff = gen().preview_apply(schema, tmp_path)
    assert "app/users.py" in diff
    assert "app/posts.py" in diff


# ---------------------------------------------------------------------------
# 7. Surgical update — preserve docstrings, relationships, comments
# ---------------------------------------------------------------------------

EXISTING_WITH_RELATIONSHIPS_SQLA = dedent("""\
    import uuid
    from typing import Optional
    from sqlalchemy import String, Uuid
    from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


    class User(Base):
        \"\"\"Application user account.\"\"\"
        __tablename__ = "users"

        id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, default=uuid.uuid4, primary_key=True)
        name: Mapped[str] = mapped_column(String(100), nullable=False)

        memberships = relationship("Membership", back_populates="user")
        # Nullable relation — AuditLog.user_id is Optional
        audit_logs = relationship("AuditLog", back_populates="user")
""")


def _users_schema_sqla(**col_overrides) -> AlterSchema:
    cols = [
        Column(name="id", type="uuid", primary_key=True, nullable=False, default="uuid4"),
        Column(name="name", type="string", nullable=False, max_length=100),
    ]
    cols_dict = {c.name: c for c in cols}
    cols_dict.update(col_overrides)
    return AlterSchema(
        orm="sqlalchemy",
        tables=[Table(name="users", columns=list(cols_dict.values()))],
    )


def test_sqla_update_preserves_docstring_when_schema_unchanged():
    schema = _users_schema_sqla()
    result = gen().update_models(schema, EXISTING_WITH_RELATIONSHIPS_SQLA)
    assert '"""Application user account."""' in result


def test_sqla_update_preserves_relationship_lines_when_schema_unchanged():
    schema = _users_schema_sqla()
    result = gen().update_models(schema, EXISTING_WITH_RELATIONSHIPS_SQLA)
    assert 'relationship("Membership"' in result
    assert "memberships" in result
    assert "audit_logs" in result


def test_sqla_update_preserves_inline_comment_when_schema_unchanged():
    schema = _users_schema_sqla()
    result = gen().update_models(schema, EXISTING_WITH_RELATIONSHIPS_SQLA)
    assert "# Nullable relation" in result


def test_sqla_update_new_column_inserted_before_relationship():
    schema = AlterSchema(
        orm="sqlalchemy",
        tables=[Table(name="users", columns=[
            Column(name="id", type="uuid", primary_key=True, nullable=False, default="uuid4"),
            Column(name="name", type="string", nullable=False, max_length=100),
            Column(name="email", type="string", nullable=False, max_length=255, unique=True),
        ])],
    )
    result = gen().update_models(schema, EXISTING_WITH_RELATIONSHIPS_SQLA)
    assert "email" in result
    assert result.index("email") < result.index("relationship(")


def test_sqla_update_new_table_does_not_touch_existing_class():
    schema = AlterSchema(
        orm="sqlalchemy",
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
    result = gen().update_models(schema, EXISTING_WITH_RELATIONSHIPS_SQLA)
    assert '"""Application user account."""' in result
    assert 'relationship("Membership"' in result
    assert "class Teams(Base)" in result


def test_sqla_update_changed_column_updates_only_that_field_line():
    schema = _users_schema_sqla(
        name=Column(name="name", type="string", nullable=False, max_length=200)
    )
    result = gen().update_models(schema, EXISTING_WITH_RELATIONSHIPS_SQLA)
    assert "String(200)" in result
    assert "String(100)" not in result
    assert '"""Application user account."""' in result
    assert 'relationship("Membership"' in result


def test_sqla_update_kwarg_order_preserved_for_unchanged_field():
    """Unchanged mapped_column() line keeps its original form verbatim."""
    schema = _users_schema_sqla(
        name=Column(name="name", type="string", nullable=False, max_length=200)  # changed
    )
    result = gen().update_models(schema, EXISTING_WITH_RELATIONSHIPS_SQLA)
    # id field: hand-written kwarg order preserved (nullable, default, primary_key — not canonical order)
    assert "mapped_column(Uuid, nullable=False, default=uuid.uuid4, primary_key=True)" in result
