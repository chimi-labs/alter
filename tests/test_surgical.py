"""Unit tests for alter.generators._surgical — the surgical class-update helpers."""

from __future__ import annotations

from textwrap import dedent

import pytest

from alter.generators._surgical import (
    _bare_field_default,
    _bare_field_equivalent,
    _class_needs_update,
    _field_kwargs_equal,
    _field_lhs,
    _get_bare_field_stmts,
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


def test_class_needs_update_field_column_deleted_from_schema_triggers():
    """A Field()-style column present in the file but absent from the schema means
    the column was deleted — the class needs updating to remove it."""
    schema_fields = [
        "    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)",
        # 'name' is in the file but not in schema_fields → deleted → must trigger
    ]
    assert _class_needs_update(CLASS_WITH_EXTRAS, schema_fields)


def test_class_needs_update_relationship_not_in_schema_does_not_trigger():
    """Relationship() lines are not schema columns — their absence from
    schema_field_lines must NOT trigger an update."""
    schema_fields = [
        "    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)",
        "    name: str = Field(max_length=100)",
        # memberships / audit_logs are Relationship() calls — not in schema_fields
        # but that is expected and must not trigger a spurious update
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


# ---------------------------------------------------------------------------
# _get_bare_field_stmts
# ---------------------------------------------------------------------------


CLASS_BARE = dedent("""\
    class Item(SQLModel, table=True):
        id: int = Field(primary_key=True)
        name: str = Field(max_length=100)
        body: str
        optional_field: Optional[str] = None
        count: int = 0
""")

CLASS_ALL_FIELD = dedent("""\
    class Item(SQLModel, table=True):
        id: int = Field(primary_key=True)
        name: str = Field(max_length=100)
        body: str = Field()
""")


def test_get_bare_field_stmts_finds_bare_annotation() -> None:
    stmts = _get_bare_field_stmts(CLASS_BARE)
    names = [s[0] for s in stmts]
    assert "body" in names


def test_get_bare_field_stmts_finds_constant_default() -> None:
    stmts = _get_bare_field_stmts(CLASS_BARE)
    names = [s[0] for s in stmts]
    assert "count" in names


def test_get_bare_field_stmts_finds_none_default() -> None:
    stmts = _get_bare_field_stmts(CLASS_BARE)
    names = [s[0] for s in stmts]
    assert "optional_field" in names


def test_get_bare_field_stmts_ignores_field_calls() -> None:
    stmts = _get_bare_field_stmts(CLASS_BARE)
    names = [s[0] for s in stmts]
    assert "id" not in names
    assert "name" not in names


def test_get_bare_field_stmts_empty_for_all_field_class() -> None:
    stmts = _get_bare_field_stmts(CLASS_ALL_FIELD)
    assert stmts == []


# ---------------------------------------------------------------------------
# _bare_field_default
# ---------------------------------------------------------------------------


def test_bare_field_default_none_for_bare_annotation() -> None:
    assert _bare_field_default("    body: str") is None


def test_bare_field_default_integer() -> None:
    assert _bare_field_default("    count: int = 0") == "0"


def test_bare_field_default_none_literal() -> None:
    assert _bare_field_default("    opt: Optional[str] = None") == "None"


def test_bare_field_default_string_literal() -> None:
    assert _bare_field_default('    status: str = "active"') == "'active'"


# ---------------------------------------------------------------------------
# _bare_field_equivalent
# ---------------------------------------------------------------------------


def test_bare_equiv_bare_annotation_vs_empty_field() -> None:
    """bare `body: str` ↔ `body: str = Field()` — equivalent."""
    assert _bare_field_equivalent("    body: str", "    body: str = Field()")


def test_bare_equiv_none_default_vs_field_default_none() -> None:
    assert _bare_field_equivalent(
        "    opt: Optional[str] = None",
        "    opt: Optional[str] = Field(default=None)",
    )


def test_bare_equiv_integer_default() -> None:
    assert _bare_field_equivalent(
        "    count: int = 0",
        "    count: int = Field(default=0)",
    )


def test_bare_not_equiv_when_generated_adds_max_length() -> None:
    assert not _bare_field_equivalent(
        "    body: str",
        "    body: str = Field(max_length=200)",
    )


def test_bare_not_equiv_when_generated_adds_unique() -> None:
    assert not _bare_field_equivalent(
        "    email: str",
        "    email: str = Field(unique=True)",
    )


def test_bare_not_equiv_different_default() -> None:
    assert not _bare_field_equivalent(
        "    count: int = 0",
        "    count: int = Field(default=5)",
    )


# ---------------------------------------------------------------------------
# _class_needs_update — bare field awareness
# ---------------------------------------------------------------------------


def test_needs_update_false_when_bare_fields_match() -> None:
    """Bare fields equivalent to generated lines → no update needed."""
    schema_lines = [
        "    id: int = Field(primary_key=True)",
        "    name: str = Field(max_length=100)",
        "    body: str = Field()",
        "    optional_field: Optional[str] = Field(default=None)",
        "    count: int = Field(default=0)",
    ]
    assert not _class_needs_update(CLASS_BARE, schema_lines)


def test_needs_update_true_when_schema_adds_constraint_to_bare_field() -> None:
    """Schema adds max_length to a previously bare field → update needed."""
    schema_lines = [
        "    id: int = Field(primary_key=True)",
        "    name: str = Field(max_length=100)",
        "    body: str = Field(max_length=500)",  # bare body: str, now needs max_length
        "    optional_field: Optional[str] = Field(default=None)",
        "    count: int = Field(default=0)",
    ]
    assert _class_needs_update(CLASS_BARE, schema_lines)


# ---------------------------------------------------------------------------
# _surgical_patch_class — no duplicate fields from bare annotations
# ---------------------------------------------------------------------------


def test_patch_class_no_duplicate_from_bare_annotation() -> None:
    """A bare `body: str` must NOT be duplicated as `body: str = Field()`."""
    schema_lines = [
        "    id: int = Field(primary_key=True)",
        "    name: str = Field(max_length=100)",
        "    body: str = Field()",
        "    optional_field: Optional[str] = Field(default=None)",
        "    count: int = Field(default=0)",
    ]
    # _surgical_patch_class is called only when _class_needs_update is True.
    # Force it directly so we can check the invariant even if needs_update=False.
    result = _surgical_patch_class(CLASS_BARE, schema_lines)
    joined = "".join(result)
    assert joined.count("body:") == 1, "body field must appear exactly once"
    assert joined.count("optional_field:") == 1
    assert joined.count("count:") == 1


def test_patch_class_bare_field_upgraded_when_schema_adds_max_length() -> None:
    """When schema adds max_length to a bare field, the bare line is replaced."""
    schema_lines = [
        "    id: int = Field(primary_key=True)",
        "    name: str = Field(max_length=100)",
        "    body: str = Field(max_length=500)",
    ]
    src = dedent("""\
        class Item(SQLModel, table=True):
            id: int = Field(primary_key=True)
            name: str = Field(max_length=100)
            body: str
    """)
    result = _surgical_patch_class(src, schema_lines)
    joined = "".join(result)
    assert joined.count("body:") == 1, "body must appear exactly once after upgrade"
    assert "max_length=500" in joined, "upgraded bare field must include max_length"
    assert "body: str = Field(max_length=500)" in joined


# ---------------------------------------------------------------------------
# surgical_update_class — end-to-end no-duplicate guarantee
# ---------------------------------------------------------------------------


CLASS_MIXED_BARE = dedent("""\
    class Post(SQLModel, table=True):
        id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
        title: str = Field(max_length=200)
        body: str
        view_count: int = 0
        published: bool = False
""")


def test_surgical_update_class_returns_none_when_bare_matches_schema() -> None:
    """If the schema is fully represented by the existing bare/Field lines, no
    update should be emitted (surgical_update_class returns None)."""
    schema_lines = [
        "    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)",
        "    title: str = Field(max_length=200)",
        "    body: str = Field()",
        "    view_count: int = Field(default=0)",
        "    published: bool = Field(default=False)",
    ]
    result = surgical_update_class(CLASS_MIXED_BARE, schema_lines)
    assert result is None, (
        "surgical_update_class should return None when bare fields match schema"
    )


def test_surgical_update_class_no_duplicate_fields_produced() -> None:
    """Even when an update IS needed, bare fields must never be duplicated."""
    # Schema adds unique=True to title — triggers an update
    schema_lines = [
        "    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)",
        "    title: str = Field(max_length=200, unique=True)",  # changed
        "    body: str = Field()",
        "    view_count: int = Field(default=0)",
        "    published: bool = Field(default=False)",
    ]
    result = surgical_update_class(CLASS_MIXED_BARE, schema_lines)
    assert result is not None
    joined = "".join(result)
    for field in ("body", "view_count", "published"):
        count = joined.count(f"{field}:")
        assert count == 1, f"Field '{field}' must appear exactly once; found {count}"
