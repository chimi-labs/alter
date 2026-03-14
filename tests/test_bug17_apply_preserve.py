"""Regression tests — alter apply must not make spurious changes for Bug 17.

BUG: ``alter apply`` made three unwanted changes when the existing code was
already semantically equivalent to the schema:

  1. ``default_factory=uuid4`` (using ``from uuid import uuid4``) was rewritten
     to ``default_factory=uuid.uuid4`` and ``import uuid`` was injected —
     even though both forms call the same function.

  2. ``default_factory=datetime.utcnow`` was already protected by the existing
     ``_DEFAULT_FACTORY_EQUIV`` table; this test suite verifies that the full
     pipeline (including ``_insert_missing_imports``) produces no spurious
     ``from datetime import timezone`` injection when the utcnow form is
     preserved.

  3. String kwarg values (e.g. ``foreign_key="user.id"``) were silently
     rewritten to single-quote form (``foreign_key='user.id'``) by
     ``ast.unparse`` when ``_rebuild_field_line`` was triggered for *any*
     kwarg change on the same field.

Fix summary:
  - ``_DEFAULT_FACTORY_EQUIV`` gained ``"uuid4": "uuid.uuid4"`` so that
    ``_field_kwargs_equal`` (and ``_normalize_kw_for_eq``) treats the two
    forms as identical.
  - ``_parse_field_kwargs_raw_text`` extracts verbatim kwarg text from the
    existing source; ``_rebuild_field_line`` now uses that raw text for any
    kwarg whose semantic value (``ast.unparse``'d) is unchanged, preserving
    the original quote style.
  - The existing ``_insert_missing_imports`` "referenced" filter correctly
    suppresses ``import uuid`` when ``uuid4`` (not ``uuid.uuid4``) appears
    in the body after a no-op surgical pass.
"""

from __future__ import annotations

import textwrap

import pytest

from alter.generators._surgical import (
    _field_kwargs_equal,
    _normalize_kw_for_eq,
    _parse_field_kwargs_raw_text,
    _rebuild_field_line,
    surgical_update_class,
)


# ---------------------------------------------------------------------------
# _normalize_kw_for_eq — uuid4 equivalence
# ---------------------------------------------------------------------------


class TestNormalizeKwForEqUuid4:
    def test_uuid4_normalised_to_uuid_uuid4(self):
        result = _normalize_kw_for_eq({"default_factory": "uuid4"})
        assert result == {"default_factory": "uuid.uuid4"}

    def test_uuid_uuid4_unchanged(self):
        result = _normalize_kw_for_eq({"default_factory": "uuid.uuid4"})
        assert result == {"default_factory": "uuid.uuid4"}

    def test_other_factory_unchanged(self):
        result = _normalize_kw_for_eq({"default_factory": "list"})
        assert result == {"default_factory": "list"}


# ---------------------------------------------------------------------------
# _field_kwargs_equal — uuid4 ↔ uuid.uuid4
# ---------------------------------------------------------------------------


class TestFieldKwargsEqualUuid4:
    def test_uuid4_equals_uuid_uuid4_same_lhs(self):
        """With matching LHS types, uuid4 ↔ uuid.uuid4 is treated as equal."""
        existing = "    id: UUID = Field(default_factory=uuid4, primary_key=True)"
        new = "    id: UUID = Field(primary_key=True, default_factory=uuid.uuid4)"
        assert _field_kwargs_equal(existing, new) is True

    def test_uuid_uuid4_equals_uuid4_same_lhs(self):
        """uuid.uuid4 ↔ uuid4 is symmetric."""
        existing = "    id: UUID = Field(primary_key=True, default_factory=uuid.uuid4)"
        new = "    id: UUID = Field(default_factory=uuid4, primary_key=True)"
        assert _field_kwargs_equal(existing, new) is True

    def test_uuid4_not_equal_to_different_factory(self):
        existing = "    id: UUID = Field(default_factory=uuid4)"
        new = "    id: UUID = Field(default_factory=list)"
        assert _field_kwargs_equal(existing, new) is False


# ---------------------------------------------------------------------------
# _parse_field_kwargs_raw_text
# ---------------------------------------------------------------------------


class TestParseFieldKwargsRawText:
    def test_double_quoted_fk_preserved(self):
        line = '    author_id: UUID = Field(foreign_key="user.id")'
        raw = _parse_field_kwargs_raw_text(line)
        assert raw.get("foreign_key") == 'foreign_key="user.id"'

    def test_single_quoted_fk_preserved(self):
        line = "    author_id: UUID = Field(foreign_key='user.id')"
        raw = _parse_field_kwargs_raw_text(line)
        assert raw.get("foreign_key") == "foreign_key='user.id'"

    def test_multiple_kwargs_extracted(self):
        line = '    x: int = Field(default=0, index=True, foreign_key="tbl.id")'
        raw = _parse_field_kwargs_raw_text(line)
        assert "default" in raw
        assert "index" in raw
        assert raw["foreign_key"] == 'foreign_key="tbl.id"'

    def test_non_field_line_returns_empty(self):
        line = "    x: int = 0"
        raw = _parse_field_kwargs_raw_text(line)
        assert raw == {}

    def test_multiline_field_extracts_kwargs(self):
        text = textwrap.dedent("""\
            author_id: UUID = Field(
                foreign_key="user.id",
                index=True,
            )""")
        raw = _parse_field_kwargs_raw_text(text)
        assert raw.get("foreign_key") == 'foreign_key="user.id"'


# ---------------------------------------------------------------------------
# _rebuild_field_line — uuid4 preserved
# ---------------------------------------------------------------------------


class TestRebuildFieldLineUuid4:
    def test_uuid4_kept_when_other_kwarg_changes(self):
        """When only index changes, uuid4 form must survive the rebuild."""
        existing = "    id: UUID = Field(default_factory=uuid4, primary_key=True)"
        new_line = "    id: UUID = Field(primary_key=True, default_factory=uuid.uuid4, index=True)"
        rebuilt = _rebuild_field_line(existing, new_line)
        assert "uuid4" in rebuilt
        assert "uuid.uuid4" not in rebuilt

    def test_uuid4_kept_when_no_other_changes(self):
        """_rebuild_field_line called with only uuid4 ↔ uuid.uuid4 difference."""
        existing = "    id: UUID = Field(default_factory=uuid4, primary_key=True)"
        new_line = "    id: UUID = Field(primary_key=True, default_factory=uuid.uuid4)"
        rebuilt = _rebuild_field_line(existing, new_line)
        assert "uuid4" in rebuilt
        assert "uuid.uuid4" not in rebuilt


# ---------------------------------------------------------------------------
# _rebuild_field_line — quote style preservation
# ---------------------------------------------------------------------------


class TestRebuildFieldLineQuoteStyle:
    def test_double_quoted_fk_preserved_when_other_kwarg_changes(self):
        """Double-quoted FK must survive rebuild when index is added."""
        existing = '    author_id: UUID = Field(foreign_key="user.id")'
        new_line = "    author_id: UUID = Field(foreign_key='user.id', index=True)"
        rebuilt = _rebuild_field_line(existing, new_line)
        assert 'foreign_key="user.id"' in rebuilt
        assert "foreign_key='user.id'" not in rebuilt

    def test_single_quoted_fk_preserved_when_other_kwarg_changes(self):
        """Single-quoted FK also preserved (not forced to double)."""
        existing = "    author_id: UUID = Field(foreign_key='user.id', index=True)"
        new_line = "    author_id: UUID = Field(foreign_key='user.id')"  # index removed
        rebuilt = _rebuild_field_line(existing, new_line)
        assert "foreign_key='user.id'" in rebuilt

    def test_unchanged_string_default_preserves_quotes(self):
        """String default value quote style preserved when a different kwarg changes."""
        existing = '    name: str = Field(default="admin", index=False)'
        new_line = "    name: str = Field(default='admin', index=True)"
        rebuilt = _rebuild_field_line(existing, new_line)
        assert 'default="admin"' in rebuilt


# ---------------------------------------------------------------------------
# surgical_update_class — uuid4 → no spurious rewrite
# ---------------------------------------------------------------------------


_SOURCE_UUID4_FORM = """\
from uuid import UUID, uuid4
from sqlmodel import SQLModel, Field

class User(SQLModel, table=True):
    __tablename__ = "users"
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(max_length=100)
"""


class TestSurgicalUpdateClassUuid4:
    def test_uuid4_not_rewritten_when_schema_unchanged(self):
        """If the only kwarg difference is uuid4 vs uuid.uuid4, surgical update is a no-op.

        Note: the schema line uses the same LHS type annotation as the existing
        source (``UUID``, matching the ``from uuid import UUID`` at the top of
        the file) so that only the kwarg value difference is tested here.
        """
        schema_lines = [
            # LHS type matches existing source — only kwarg value differs
            "    id: UUID = Field(primary_key=True, default_factory=uuid.uuid4)",
            "    name: str = Field(max_length=100)",
        ]
        result = surgical_update_class(_SOURCE_UUID4_FORM, schema_lines)
        assert result is None, (
            "surgical_update_class should return None (no-op) when uuid4 "
            "is equivalent to uuid.uuid4"
        )

    def test_uuid4_preserved_when_new_column_added(self):
        """When a new column is added, uuid4 must not be rewritten."""
        schema_lines = [
            "    id: uuid.UUID = Field(primary_key=True, default_factory=uuid.uuid4)",
            "    name: str = Field(max_length=100)",
            "    email: str = Field(max_length=255)",
        ]
        result = surgical_update_class(_SOURCE_UUID4_FORM, schema_lines)
        assert result is not None  # new column means update needed
        joined = "".join(result)
        # uuid4 form must be preserved; only the new column line should differ
        assert "default_factory=uuid4" in joined
        assert "default_factory=uuid.uuid4" not in joined


# ---------------------------------------------------------------------------
# Integration — update_models must not add spurious imports
# ---------------------------------------------------------------------------


_SOURCE_UUID4_COMPLETE = """\
from uuid import UUID, uuid4
from sqlmodel import SQLModel, Field

class User(SQLModel, table=True):
    __tablename__ = "users"
    id: UUID = Field(default_factory=uuid4, primary_key=True)
"""

_SOURCE_UTCNOW_COMPLETE = """\
from datetime import datetime
from sqlmodel import SQLModel, Field

class Event(SQLModel, table=True):
    __tablename__ = "events"
    id: int = Field(primary_key=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
"""


class TestUpdateModelsNoSpuriousImports:
    def test_import_uuid_not_added_when_uuid4_form_used(self):
        """update_models must not inject ``import uuid`` when uuid4 is already present."""
        from alter.generators.sqlmodel import SQLModelGenerator
        from alter.schema import AlterSchema, Column, Table

        schema = AlterSchema(
            tables=[
                Table(
                    name="users",
                    columns=[
                        Column(
                            name="id",
                            type="uuid",
                            primary_key=True,
                            nullable=False,
                            default="uuid4",
                        ),
                    ],
                )
            ]
        )
        gen = SQLModelGenerator()
        result = gen.update_models(schema, _SOURCE_UUID4_COMPLETE)
        assert result == _SOURCE_UUID4_COMPLETE, (
            "update_models must not add 'import uuid' when uuid4 form is in use\n"
            + "\n".join(
                __import__("difflib").unified_diff(
                    _SOURCE_UUID4_COMPLETE.splitlines(),
                    result.splitlines(),
                    lineterm="",
                )
            )
        )

    def test_timezone_not_added_when_utcnow_form_used(self):
        """update_models must not inject ``from datetime import timezone`` for utcnow."""
        from alter.generators.sqlmodel import SQLModelGenerator
        from alter.schema import AlterSchema, Column, Table

        schema = AlterSchema(
            tables=[
                Table(
                    name="events",
                    columns=[
                        Column(name="id", type="int", primary_key=True, nullable=False),
                        Column(
                            name="created_at",
                            type="datetime",
                            nullable=False,
                            default="utcnow",
                        ),
                    ],
                )
            ]
        )
        gen = SQLModelGenerator()
        result = gen.update_models(schema, _SOURCE_UTCNOW_COMPLETE)
        assert result == _SOURCE_UTCNOW_COMPLETE, (
            "update_models must not add 'from datetime import timezone' when "
            "datetime.utcnow form is preserved\n"
            + "\n".join(
                __import__("difflib").unified_diff(
                    _SOURCE_UTCNOW_COMPLETE.splitlines(),
                    result.splitlines(),
                    lineterm="",
                )
            )
        )


# ---------------------------------------------------------------------------
# Integration — quote style preserved in full update_models round-trip
# ---------------------------------------------------------------------------


_SOURCE_DOUBLE_QUOTE_FK = """\
from uuid import UUID
from typing import Optional
from sqlmodel import SQLModel, Field

class Post(SQLModel, table=True):
    __tablename__ = "posts"
    id: int = Field(primary_key=True)
    author_id: UUID = Field(foreign_key="user.id")
    title: str = Field(max_length=200)
"""


class TestUpdateModelsQuoteStyle:
    def test_double_quoted_fk_preserved_when_schema_unchanged(self):
        """update_models must not alter double-quoted FK strings."""
        from alter.generators.sqlmodel import SQLModelGenerator
        from alter.schema import AlterSchema, Column, Table

        schema = AlterSchema(
            tables=[
                Table(
                    name="posts",
                    columns=[
                        Column(name="id", type="int", primary_key=True, nullable=False),
                        Column(
                            name="author_id",
                            type="uuid",
                            nullable=False,
                            foreign_key="user.id",
                        ),
                        Column(
                            name="title",
                            type="string",
                            nullable=False,
                            max_length=200,
                        ),
                    ],
                )
            ]
        )
        gen = SQLModelGenerator()
        result = gen.update_models(schema, _SOURCE_DOUBLE_QUOTE_FK)
        assert result == _SOURCE_DOUBLE_QUOTE_FK, (
            "update_models must not change double-quoted FK to single-quoted\n"
            + "\n".join(
                __import__("difflib").unified_diff(
                    _SOURCE_DOUBLE_QUOTE_FK.splitlines(),
                    result.splitlines(),
                    lineterm="",
                )
            )
        )

    def test_double_quoted_fk_preserved_when_other_column_changes(self):
        """FK quote style survives when a *different* column gets a new kwarg."""
        from alter.generators.sqlmodel import SQLModelGenerator
        from alter.schema import AlterSchema, Column, Table

        schema = AlterSchema(
            tables=[
                Table(
                    name="posts",
                    columns=[
                        Column(name="id", type="int", primary_key=True, nullable=False),
                        Column(
                            name="author_id",
                            type="uuid",
                            nullable=False,
                            foreign_key="user.id",
                        ),
                        Column(
                            name="title",
                            type="string",
                            nullable=False,
                            max_length=200,
                            index=True,  # NEW — triggers class update
                        ),
                    ],
                )
            ]
        )
        gen = SQLModelGenerator()
        result = gen.update_models(schema, _SOURCE_DOUBLE_QUOTE_FK)
        assert result != _SOURCE_DOUBLE_QUOTE_FK  # a change was made (index added)
        assert 'foreign_key="user.id"' in result, (
            "Double-quoted FK must be preserved even when another column changes"
        )
        assert "foreign_key='user.id'" not in result
