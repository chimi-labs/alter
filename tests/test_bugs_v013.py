"""Regression tests for the five bugs fixed in v0.1.3.

Bug 1 — Schema-qualified foreign keys stripped
Bug 2 — Optional[list] becomes Optional[dict]
Bug 3 — Optional[str] PKs forced to non-Optional
Bug 4 — Multi-line Field() calls collapsed to single line
Bug 5 — Field() kwarg order changed on replacement
"""

from __future__ import annotations

import tempfile
import os
from pathlib import Path
from textwrap import dedent

import pytest

from alter.parsers.sqlmodel import SQLModelParser
from alter.generators.sqlmodel import SQLModelGenerator
from alter.generators._surgical import (
    _field_kwargs_equal,
    _rebuild_field_line,
    surgical_update_class,
)
from alter.schema import AlterSchema, Column, Table
from alter.types import python_to_alter, alter_to_python


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_source(source: str):
    fd, name = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    p = Path(name)
    p.write_text(dedent(source))
    try:
        parser = SQLModelParser()
        return parser._parse_file_internal(p)
    finally:
        p.unlink()


def _update_source(source: str, schema: AlterSchema) -> str:
    return SQLModelGenerator().update_models(schema, dedent(source))


# ---------------------------------------------------------------------------
# Bug 1 — Schema-qualified foreign keys preserved verbatim
# ---------------------------------------------------------------------------

class TestBug1SchemaQualifiedFK:
    def test_parser_preserves_schema_prefix(self):
        """foreign_key="alpha_ai.sessions.id" must be stored without stripping."""
        source = """\
            import uuid
            from typing import Optional
            from sqlmodel import Field, SQLModel

            class Event(SQLModel, table=True):
                __tablename__ = "events"
                id: uuid.UUID = Field(primary_key=True, default_factory=uuid.uuid4)
                session_id: Optional[uuid.UUID] = Field(
                    default=None, foreign_key="alpha_ai.sessions.id"
                )
        """
        result = _parse_source(source)
        event = next(t for t in result.tables if t.name == "events")
        session_col = next(c for c in event.columns if c.name == "session_id")
        assert session_col.foreign_key == "alpha_ai.sessions.id", (
            f"Expected 'alpha_ai.sessions.id', got {session_col.foreign_key!r}"
        )

    def test_parser_unqualified_fk_unchanged(self):
        """Standard 'table.column' FK must be unchanged."""
        source = """\
            import uuid
            from sqlmodel import Field, SQLModel

            class Post(SQLModel, table=True):
                __tablename__ = "posts"
                id: uuid.UUID = Field(primary_key=True)
                user_id: uuid.UUID = Field(foreign_key="users.id")
        """
        result = _parse_source(source)
        post = next(t for t in result.tables if t.name == "posts")
        uid = next(c for c in post.columns if c.name == "user_id")
        assert uid.foreign_key == "users.id"

    def test_relation_to_table_unqualified(self):
        """Relation.to_table should hold the bare table name (no schema prefix)."""
        source = """\
            import uuid
            from typing import Optional
            from sqlmodel import Field, SQLModel

            class Event(SQLModel, table=True):
                __tablename__ = "events"
                id: uuid.UUID = Field(primary_key=True)
                session_id: Optional[uuid.UUID] = Field(
                    default=None, foreign_key="alpha_ai.sessions.id"
                )
        """
        result = _parse_source(source)
        rel = next(
            (r for r in result.relations if r.from_column == "session_id"), None
        )
        assert rel is not None, "Relation should be created"
        assert rel.to_table == "sessions", (
            f"Expected 'sessions', got {rel.to_table!r}"
        )
        assert rel.to_column == "id"

    def test_generator_roundtrip_schema_prefix(self):
        """full round-trip: schema-prefixed FK must survive parse → generate → parse."""
        source = """\
            import uuid
            from typing import Optional
            from sqlmodel import Field, SQLModel

            class Event(SQLModel, table=True):
                __tablename__ = "events"
                id: uuid.UUID = Field(primary_key=True, default_factory=uuid.uuid4)
                session_id: Optional[uuid.UUID] = Field(
                    default=None, foreign_key="alpha_ai.sessions.id"
                )
        """
        result1 = _parse_source(source)
        schema = AlterSchema(
            orm="sqlmodel", tables=result1.tables, enums=result1.enums
        )
        generated = SQLModelGenerator().generate_models(schema)
        result2 = _parse_source(generated)
        event2 = next(t for t in result2.tables if t.name == "events")
        sid = next(c for c in event2.columns if c.name == "session_id")
        assert sid.foreign_key == "alpha_ai.sessions.id", (
            f"FK changed after round-trip: {sid.foreign_key!r}"
        )

    def test_apply_does_not_change_schema_fk(self):
        """update_models must not touch a schema-qualified FK field."""
        existing = """\
            import uuid
            from typing import Optional
            from sqlmodel import Field, SQLModel

            class Event(SQLModel, table=True):
                __tablename__ = "events"
                id: uuid.UUID = Field(primary_key=True, default_factory=uuid.uuid4)
                session_id: Optional[uuid.UUID] = Field(
                    default=None, foreign_key="alpha_ai.sessions.id"
                )
        """
        result = _parse_source(existing)
        schema = AlterSchema(
            orm="sqlmodel", tables=result.tables, enums=result.enums
        )
        updated = _update_source(existing, schema)
        assert updated == dedent(existing), (
            f"update_models changed unchanged file:\n{updated!r}"
        )


# ---------------------------------------------------------------------------
# Bug 2 — Optional[list] round-trips correctly (not as Optional[dict])
# ---------------------------------------------------------------------------

class TestBug2ListType:
    def test_types_list_maps_to_json_array(self):
        assert python_to_alter("list") == "json_array"
        assert python_to_alter("List") == "json_array"

    def test_types_json_array_maps_to_list(self):
        assert alter_to_python("json_array") == "list"

    def test_types_dict_still_maps_to_json(self):
        assert python_to_alter("dict") == "json"
        assert alter_to_python("json") == "dict"

    def test_parser_bare_list_becomes_json_array(self):
        source = """\
            from typing import Optional
            from sqlmodel import Field, SQLModel

            class Item(SQLModel, table=True):
                __tablename__ = "items"
                id: int = Field(primary_key=True)
                tags: Optional[list] = Field(default=None)
        """
        result = _parse_source(source)
        item = next(t for t in result.tables if t.name == "items")
        tags = next(c for c in item.columns if c.name == "tags")
        assert tags.type == "json_array", f"Expected 'json_array', got {tags.type!r}"

    def test_generator_json_array_emits_list(self):
        """json_array columns must be emitted as list, not dict."""
        col = Column(name="tags", type="json_array", nullable=True)
        table = Table(name="items", columns=[col])
        schema = AlterSchema(orm="sqlmodel", tables=[table])
        generated = SQLModelGenerator().generate_models(schema)
        assert "Optional[list]" in generated, (
            f"Expected 'Optional[list]' in generated code:\n{generated}"
        )
        assert "Optional[dict]" not in generated

    def test_roundtrip_optional_list(self):
        """Optional[list] must survive the full round-trip."""
        source = """\
            from typing import Optional
            from sqlmodel import Field, SQLModel

            class Item(SQLModel, table=True):
                __tablename__ = "items"
                id: int = Field(primary_key=True)
                tags: Optional[list] = Field(default=None)
        """
        result1 = _parse_source(source)
        schema = AlterSchema(
            orm="sqlmodel", tables=result1.tables, enums=result1.enums
        )
        generated = SQLModelGenerator().generate_models(schema)
        result2 = _parse_source(generated)
        item2 = next(t for t in result2.tables if t.name == "items")
        tags2 = next(c for c in item2.columns if c.name == "tags")
        assert tags2.type == "json_array", (
            f"After round-trip: expected 'json_array', got {tags2.type!r}"
        )


# ---------------------------------------------------------------------------
# Bug 3 — Optional[str] PKs not forced to str
# ---------------------------------------------------------------------------

class TestBug3OptionalPK:
    def test_field_kwargs_equal_optional_pk(self):
        """Optional[str] PK existing vs str PK schema must be considered equal."""
        existing = "    id: Optional[str] = Field(primary_key=True)"
        new =      "    id: str = Field(primary_key=True)"
        assert _field_kwargs_equal(existing, new), (
            "Optional[str] PK should be semantically equal to str PK"
        )

    def test_field_kwargs_not_equal_optional_non_pk(self):
        """Optional vs non-Optional on a non-PK field IS a real change."""
        existing = "    name: Optional[str] = Field(default=None)"
        new =      "    name: str = Field()"
        assert not _field_kwargs_equal(existing, new)

    def test_update_models_does_not_touch_optional_pk(self):
        """update_models must leave Optional[str] PK annotation unchanged."""
        existing = """\
            from typing import Optional
            from sqlmodel import Field, SQLModel

            class Session(SQLModel, table=True):
                __tablename__ = "sessions"
                id: Optional[str] = Field(
                    default_factory=lambda: str(__import__('uuid').uuid4()),
                    primary_key=True
                )
                name: str = Field()
        """
        result = _parse_source(existing)
        schema = AlterSchema(
            orm="sqlmodel", tables=result.tables, enums=result.enums
        )
        updated = _update_source(existing, schema)
        # The Optional[str] annotation must survive
        assert "id: Optional[str]" in updated, (
            f"Optional[str] PK annotation was changed:\n{updated}"
        )

    def test_new_pk_field_gets_non_optional(self):
        """Newly generated PK fields should NOT have Optional."""
        col = Column(name="id", type="string", primary_key=True, nullable=False)
        table = Table(name="t", columns=[col])
        schema = AlterSchema(orm="sqlmodel", tables=[table])
        generated = SQLModelGenerator().generate_models(schema)
        # fresh generation must emit non-optional PK
        assert "id: str = Field(primary_key=True)" in generated


# ---------------------------------------------------------------------------
# Bug 4 — Multi-line Field() calls preserved
# ---------------------------------------------------------------------------

class TestBug4MultilinePreservation:
    def test_rebuild_preserves_multiline_when_changed(self):
        """If a field changes but was multi-line, replacement should be multi-line."""
        existing = (
            "    id: Optional[str] = Field(\n"
            "        default_factory=lambda: str(uuid.uuid4()), primary_key=True\n"
            "    )"
        )
        # Schema says nullable changed — but the field IS a PK so it shouldn't
        # change; still, let's test rebuild_field_line directly with a fictional change.
        new_schema = "    id: str = Field(primary_key=True, default_factory=lambda: str(uuid.uuid4()), unique=True)"
        rebuilt = _rebuild_field_line(existing, new_schema)
        # Result should span multiple lines (since original did)
        assert "\n" in rebuilt, f"Expected multi-line result:\n{rebuilt!r}"
        assert "unique=True" in rebuilt  # new kwarg added

    def test_update_models_leaves_multiline_unchanged(self):
        """A multi-line field that hasn't changed must not be collapsed."""
        existing = """\
            import uuid
            from typing import Optional
            from sqlmodel import Field, SQLModel

            class Session(SQLModel, table=True):
                __tablename__ = "sessions"
                id: Optional[str] = Field(
                    default_factory=lambda: str(uuid.uuid4()), primary_key=True
                )
        """
        result = _parse_source(existing)
        schema = AlterSchema(
            orm="sqlmodel", tables=result.tables, enums=result.enums
        )
        updated = _update_source(existing, schema)
        assert updated == dedent(existing), (
            f"Multi-line field was modified:\n{updated!r}"
        )


# ---------------------------------------------------------------------------
# Bug 5 — Field() kwarg order preserved on replacement
# ---------------------------------------------------------------------------

class TestBug5KwargOrderPreservation:
    def test_rebuild_preserves_kwarg_order(self):
        """When rebuilding a changed field, original kwarg order is kept."""
        existing = "    id: str = Field(default_factory=uuid.uuid4, primary_key=True)"
        # Schema adds unique=True (a new kwarg), but keeps others
        new_schema = "    id: str = Field(primary_key=True, default_factory=uuid.uuid4, unique=True)"
        rebuilt = _rebuild_field_line(existing, new_schema)
        # default_factory should still appear before primary_key (original order)
        df_pos = rebuilt.index("default_factory")
        pk_pos = rebuilt.index("primary_key")
        assert df_pos < pk_pos, (
            f"default_factory should come before primary_key in:\n{rebuilt!r}"
        )

    def test_unchanged_field_keeps_kwarg_order(self):
        """A field that hasn't changed must be emitted verbatim."""
        existing = "    id: str = Field(default_factory=uuid.uuid4, primary_key=True)"
        new =      "    id: str = Field(primary_key=True, default_factory=uuid.uuid4)"
        # These are kwargs-equal (order differs only)
        assert _field_kwargs_equal(existing, new)
        # surgical_update_class should not touch this field
        class_src = dedent("""\
            class Foo(SQLModel, table=True):
                __tablename__ = "foo"
                id: str = Field(default_factory=uuid.uuid4, primary_key=True)
        """)
        result = surgical_update_class(class_src, [new])
        assert result is None, "Field with same kwargs (different order) must not trigger update"

    def test_update_models_does_not_reorder_kwargs(self):
        """update_models on an unchanged file must return identical content."""
        existing = """\
            import uuid
            from sqlmodel import Field, SQLModel

            class Widget(SQLModel, table=True):
                __tablename__ = "widgets"
                id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
                name: str = Field(max_length=100)
        """
        result = _parse_source(existing)
        schema = AlterSchema(
            orm="sqlmodel", tables=result.tables, enums=result.enums
        )
        updated = _update_source(existing, schema)
        assert updated == dedent(existing), (
            f"Kwarg order changed unexpectedly:\n{updated!r}"
        )


# ---------------------------------------------------------------------------
# Integration — adding a new table must not touch existing fields
# ---------------------------------------------------------------------------

class TestMinimalDiffPrinciple:
    def test_add_table_does_not_touch_existing_fields(self):
        """Adding a new table should leave all existing fields byte-for-byte identical."""
        existing = """\
            import uuid
            from typing import Optional
            from sqlmodel import Field, SQLModel

            class Session(SQLModel, table=True):
                __tablename__ = "sessions"
                id: Optional[str] = Field(
                    default_factory=lambda: str(uuid.uuid4()), primary_key=True
                )
                user_id: Optional[uuid.UUID] = Field(
                    default=None, foreign_key="alpha_ai.users.id"
                )
                tags: Optional[list] = Field(default=None)
        """
        result = _parse_source(existing)
        # Now add a new table to the schema
        new_table = Table(name="events", columns=[
            Column(name="id", type="uuid", primary_key=True, nullable=False, default="uuid4"),
        ])
        schema = AlterSchema(
            orm="sqlmodel",
            tables=result.tables + [new_table],
            enums=result.enums,
        )
        updated = _update_source(existing, schema)

        # The new table must appear
        assert "class Events(SQLModel, table=True)" in updated or "events" in updated

        # All existing field lines must be unchanged
        existing_lines = dedent(existing).splitlines()
        updated_lines = updated.splitlines()

        # Check specific field lines that were prone to corruption
        for line in [
            'id: Optional[str] = Field(',
            'default_factory=lambda: str(uuid.uuid4()), primary_key=True',
            'foreign_key="alpha_ai.users.id"',
            'tags: Optional[list] = Field(default=None)',
        ]:
            assert any(line.strip() in ul for ul in updated_lines), (
                f"Line was lost or changed:\n  {line!r}\nUpdated:\n{updated}"
            )
