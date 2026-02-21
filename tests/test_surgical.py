"""Unit tests for alter.generators._surgical — the surgical class-update helpers."""

from __future__ import annotations

from textwrap import dedent

import pytest

from alter.generators._surgical import (
    _class_needs_update,
    _field_kwargs_equal,
    _field_lhs,
    _get_field_stmts,
    _parse_field_kwargs,
    _surgical_patch_class,
    surgical_update_class,
)


# ---------------------------------------------------------------------------
# _get_field_stmts
# ---------------------------------------------------------------------------

CLASS_SIMPLE = dedent("""\
    class User(SQLModel, table=True):
        __tablename__ = "users"

        id: uuid.UUID = Field(primary_key=True, default_factory=uuid.uuid4)
        name: str = Field(max_length=100)
""")

CLASS_WITH_EXTRAS = dedent("""\
    class User(SQLModel, table=True):
        \"\"\"Application user account.\"\"\"
        __tablename__ = "users"

        id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
        name: str = Field(max_length=100)

        memberships: list["Membership"] = Relationship(back_populates="user")
        # Nullable relation — AuditLog.user_id is Optional
        audit_logs: list["AuditLog"] = Relationship(back_populates="user")
""")

CLASS_SA = dedent("""\
    class User(Base):
        __tablename__ = "users"

        id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
        email: Mapped[str] = mapped_column(String(255), unique=True)
""")


def test_get_field_stmts_finds_field_calls():
    stmts = _get_field_stmts(CLASS_SIMPLE)
    names = [s[0] for s in stmts]
    assert names == ["id", "name"]


def test_get_field_stmts_skips_relationship_and_tablename():
    stmts = _get_field_stmts(CLASS_WITH_EXTRAS)
    names = [s[0] for s in stmts]
    assert names == ["id", "name"]
    assert "memberships" not in names
    assert "audit_logs" not in names


def test_get_field_stmts_handles_mapped_column():
    stmts = _get_field_stmts(CLASS_SA)
    names = [s[0] for s in stmts]
    assert names == ["id", "email"]


def test_get_field_stmts_returns_valid_line_ranges():
    stmts = _get_field_stmts(CLASS_SIMPLE)
    lines = CLASS_SIMPLE.splitlines(keepends=True)
    for col_name, start, end in stmts:
        text = "".join(lines[start - 1 : end])
        assert col_name in text


def test_get_field_stmts_empty_class():
    src = dedent("""\
        class Empty(SQLModel, table=True):
            __tablename__ = "empty"
    """)
    assert _get_field_stmts(src) == []


# ---------------------------------------------------------------------------
# _parse_field_kwargs
# ---------------------------------------------------------------------------

def test_parse_field_kwargs_simple():
    line = "    id: uuid.UUID = Field(primary_key=True, default_factory=uuid.uuid4)"
    kw = _parse_field_kwargs(line)
    assert kw is not None
    assert kw["primary_key"] == "True"
    assert kw["default_factory"] == "uuid.uuid4"


def test_parse_field_kwargs_string_value():
    line = '    fk: uuid.UUID = Field(foreign_key="users.id")'
    kw = _parse_field_kwargs(line)
    assert kw is not None
    assert "foreign_key" in kw


def test_parse_field_kwargs_empty_call():
    line = "    x: int = Field()"
    kw = _parse_field_kwargs(line)
    assert kw == {}


def test_parse_field_kwargs_mapped_column():
    line = "    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)"
    kw = _parse_field_kwargs(line)
    assert kw is not None
    assert kw["primary_key"] == "True"


def test_parse_field_kwargs_returns_none_on_non_field_line():
    line = '    memberships: list["Membership"] = Relationship(back_populates="user")'
    kw = _parse_field_kwargs(line)
    assert kw is None


# ---------------------------------------------------------------------------
# _field_kwargs_equal
# ---------------------------------------------------------------------------

def test_field_kwargs_equal_identical_lines():
    line = "    id: uuid.UUID = Field(primary_key=True, default_factory=uuid.uuid4)"
    assert _field_kwargs_equal(line, line)


def test_field_kwargs_equal_different_kwarg_order():
    a = "    id: uuid.UUID = Field(primary_key=True, default_factory=uuid.uuid4)"
    b = "    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)"
    assert _field_kwargs_equal(a, b)


def test_field_kwargs_not_equal_different_value():
    a = "    name: str = Field(max_length=100)"
    b = "    name: str = Field(max_length=200)"
    assert not _field_kwargs_equal(a, b)


def test_field_kwargs_not_equal_different_col_name():
    a = "    email: str = Field(max_length=100)"
    b = "    name: str = Field(max_length=100)"
    assert not _field_kwargs_equal(a, b)


def test_field_kwargs_not_equal_different_type():
    a = "    name: str = Field(max_length=100)"
    b = "    name: Optional[str] = Field(max_length=100)"
    assert not _field_kwargs_equal(a, b)


def test_field_kwargs_equal_ignores_leading_whitespace_difference():
    a = "    id: uuid.UUID = Field(primary_key=True)"
    b = "    id: uuid.UUID = Field(primary_key=True)"
    assert _field_kwargs_equal(a, b)


# ---------------------------------------------------------------------------
# _class_needs_update
# ---------------------------------------------------------------------------

def test_class_needs_update_no_change_ignores_kwarg_order():
    """Kwarg reorder alone must NOT trigger an update."""
    schema_fields = [
        "    id: uuid.UUID = Field(primary_key=True, default_factory=uuid.uuid4)",
        "    name: str = Field(max_length=100)",
    ]
    # Existing file has kwargs in hand-written order
    assert not _class_needs_update(CLASS_WITH_EXTRAS, schema_fields)


def test_class_needs_update_no_change_ignores_docstring_and_relationships():
    schema_fields = [
        "    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)",
        "    name: str = Field(max_length=100)",
    ]
    assert not _class_needs_update(CLASS_WITH_EXTRAS, schema_fields)


def test_class_needs_update_changed_field():
    schema_fields = [
        "    id: uuid.UUID = Field(primary_key=True, default_factory=uuid.uuid4)",
        "    name: str = Field(max_length=200)",  # changed from 100
    ]
    assert _class_needs_update(CLASS_WITH_EXTRAS, schema_fields)


def test_class_needs_update_new_column():
    schema_fields = [
        "    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)",
        "    name: str = Field(max_length=100)",
        "    email: str = Field(max_length=255)",  # NEW
    ]
    assert _class_needs_update(CLASS_WITH_EXTRAS, schema_fields)


def test_class_needs_update_extra_column_in_file_not_in_schema_does_not_trigger():
    """Columns in the file that are not in the schema should be left alone."""
    schema_fields = [
        "    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)",
        # 'name' is in the file but not in schema_fields — should NOT trigger update
    ]
    assert not _class_needs_update(CLASS_WITH_EXTRAS, schema_fields)


# ---------------------------------------------------------------------------
# _surgical_patch_class
# ---------------------------------------------------------------------------

def test_surgical_patch_preserves_docstring():
    schema_fields = [
        "    id: uuid.UUID = Field(primary_key=True, default_factory=uuid.uuid4)",
        "    name: str = Field(max_length=200)",  # changed
    ]
    result = "".join(_surgical_patch_class(CLASS_WITH_EXTRAS, schema_fields))
    assert '"""Application user account."""' in result


def test_surgical_patch_preserves_relationships():
    schema_fields = [
        "    id: uuid.UUID = Field(primary_key=True, default_factory=uuid.uuid4)",
        "    name: str = Field(max_length=200)",  # changed
    ]
    result = "".join(_surgical_patch_class(CLASS_WITH_EXTRAS, schema_fields))
    assert 'Relationship(back_populates="user")' in result
    assert "memberships" in result
    assert "audit_logs" in result


def test_surgical_patch_preserves_inline_comment():
    schema_fields = [
        "    id: uuid.UUID = Field(primary_key=True, default_factory=uuid.uuid4)",
        "    name: str = Field(max_length=100)",
        "    email: str = Field(max_length=255)",  # new column
    ]
    result = "".join(_surgical_patch_class(CLASS_WITH_EXTRAS, schema_fields))
    assert "# Nullable relation" in result


def test_surgical_patch_updates_changed_field():
    schema_fields = [
        "    id: uuid.UUID = Field(primary_key=True, default_factory=uuid.uuid4)",
        "    name: str = Field(max_length=200)",  # changed from 100
    ]
    result = "".join(_surgical_patch_class(CLASS_WITH_EXTRAS, schema_fields))
    assert "max_length=200" in result
    assert "max_length=100" not in result


def test_surgical_patch_preserves_kwarg_order_for_unchanged_field():
    """An unchanged field keeps its hand-written kwarg order verbatim."""
    schema_fields = [
        # Schema has canonical order (pk first), but existing file has default_factory first
        "    id: uuid.UUID = Field(primary_key=True, default_factory=uuid.uuid4)",
        "    name: str = Field(max_length=200)",  # changed
    ]
    result = "".join(_surgical_patch_class(CLASS_WITH_EXTRAS, schema_fields))
    # Hand-written order preserved for unchanged 'id' field
    assert "Field(default_factory=uuid.uuid4, primary_key=True)" in result


def test_surgical_patch_new_column_inserted_before_relationship():
    schema_fields = [
        "    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)",
        "    name: str = Field(max_length=100)",
        "    email: str = Field(max_length=255)",  # NEW
    ]
    result = "".join(_surgical_patch_class(CLASS_WITH_EXTRAS, schema_fields))
    assert "email" in result
    assert result.index("email") < result.index("Relationship(")


def test_surgical_patch_new_column_on_class_with_no_existing_fields():
    src = dedent("""\
        class Empty(SQLModel, table=True):
            __tablename__ = "empty"
    """)
    schema_fields = ["    id: uuid.UUID = Field(primary_key=True)"]
    result = "".join(_surgical_patch_class(src, schema_fields))
    assert "id" in result


# ---------------------------------------------------------------------------
# surgical_update_class (entry point)
# ---------------------------------------------------------------------------

def test_surgical_update_class_returns_none_when_unchanged():
    schema_fields = [
        "    id: uuid.UUID = Field(primary_key=True, default_factory=uuid.uuid4)",
        "    name: str = Field(max_length=100)",
    ]
    assert surgical_update_class(CLASS_WITH_EXTRAS, schema_fields) is None


def test_surgical_update_class_returns_lines_when_changed():
    schema_fields = [
        "    id: uuid.UUID = Field(primary_key=True, default_factory=uuid.uuid4)",
        "    name: str = Field(max_length=200)",  # changed
    ]
    result = surgical_update_class(CLASS_WITH_EXTRAS, schema_fields)
    assert result is not None
    assert isinstance(result, list)
    joined = "".join(result)
    assert "max_length=200" in joined


def test_surgical_update_class_returns_lines_when_new_column():
    schema_fields = [
        "    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)",
        "    name: str = Field(max_length=100)",
        "    email: str = Field(max_length=255)",  # NEW
    ]
    result = surgical_update_class(CLASS_WITH_EXTRAS, schema_fields)
    assert result is not None
    joined = "".join(result)
    assert "email" in joined
