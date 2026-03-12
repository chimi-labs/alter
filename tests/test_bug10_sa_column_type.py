"""Regression tests for Fix 10 / Fix 10b — sa_column type handling.

ORIGINAL FIX 10: ``_parse_field_call`` promoted the alter type to "json"
when ``sa_column=Column(JSON)`` was present, even if the Python annotation
was something else (e.g. ``Optional[str]``).

FIX 10b (current): The Python type annotation is the authoritative source.
``sa_column`` / ``sa_type`` are preserved in ``extra_kwargs`` and used at
the database level, but they do NOT override the annotation-derived type.
Exception: ``SQLEnum(MyEnum)`` inside ``sa_column`` still overrides the type
when the annotation alone does not reveal which enum class is used.

Behaviour:
- ``Optional[str]`` + ``sa_column=Column(JSON)``  → alter type "string"
- ``Optional[dict]`` + ``sa_column=Column(JSON)`` → alter type "json" (from annotation)
- ``Optional[str]`` + ``sa_column=Column(SQLEnum(Role))`` → alter type "Role"
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from textwrap import dedent

from alter.parsers.sqlmodel import SQLModelParser


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _parse(source: str):
    fd, name = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    p = Path(name)
    p.write_text(dedent(source))
    try:
        return SQLModelParser()._parse_file_internal(p)
    finally:
        p.unlink()


# ---------------------------------------------------------------------------
# JSON / JSONB override
# ---------------------------------------------------------------------------


class TestSaColumnJsonOverride:
    def test_optional_str_with_sa_column_json(self):
        """str annotation + sa_column=Column(JSON) → annotation wins → "string"."""
        source = """\
            from typing import Optional
            from sqlmodel import Field, SQLModel
            from sqlalchemy import Column, JSON

            class Widget(SQLModel, table=True):
                __tablename__ = "widgets"
                id: int = Field(primary_key=True)
                data: Optional[str] = Field(default=None, sa_column=Column(JSON))
        """
        result = _parse(source)
        col = next(c for c in result.tables[0].columns if c.name == "data")
        assert col.type == "string", f"expected 'string', got {col.type!r}"

    def test_optional_str_with_sa_column_jsonb(self):
        """str annotation + sa_column=Column(JSONB) → annotation wins → "string"."""
        source = """\
            from typing import Optional
            from sqlmodel import Field, SQLModel
            from sqlalchemy.dialects.postgresql import JSONB

            class Config(SQLModel, table=True):
                __tablename__ = "configs"
                id: int = Field(primary_key=True)
                settings: Optional[str] = Field(default=None, sa_column=Column(JSONB))
        """
        result = _parse(source)
        col = next(c for c in result.tables[0].columns if c.name == "settings")
        assert col.type == "string", f"expected 'string', got {col.type!r}"

    def test_sa_column_json_not_nullable(self):
        """Non-optional str + sa_column=Column(JSON) → annotation wins → "string"."""
        source = """\
            from sqlmodel import Field, SQLModel
            from sqlalchemy import Column, JSON

            class Log(SQLModel, table=True):
                __tablename__ = "logs"
                id: int = Field(primary_key=True)
                payload: str = Field(sa_column=Column(JSON))
        """
        result = _parse(source)
        col = next(c for c in result.tables[0].columns if c.name == "payload")
        assert col.type == "string"

    def test_json_annotation_not_double_overridden(self):
        """dict annotation (→ json) + sa_column=Column(JSON) stays "json"."""
        source = """\
            from typing import Optional
            from sqlmodel import Field, SQLModel
            from sqlalchemy import Column, JSON

            class Event(SQLModel, table=True):
                __tablename__ = "events"
                id: int = Field(primary_key=True)
                meta: Optional[dict] = Field(default=None, sa_column=Column(JSON))
        """
        result = _parse(source)
        col = next(c for c in result.tables[0].columns if c.name == "meta")
        assert col.type == "json"

    def test_sa_column_preserved_in_extra_kwargs(self):
        """sa_column is stored in extra_kwargs; annotation type is preserved."""
        source = """\
            from typing import Optional
            from sqlmodel import Field, SQLModel
            from sqlalchemy import Column, JSON

            class Item(SQLModel, table=True):
                __tablename__ = "items"
                id: int = Field(primary_key=True)
                blob: Optional[str] = Field(default=None, sa_column=Column(JSON))
        """
        result = _parse(source)
        col = next(c for c in result.tables[0].columns if c.name == "blob")
        assert col.type == "string"  # annotation wins
        assert col.extra_kwargs is not None
        assert "sa_column" in col.extra_kwargs

    def test_no_sa_column_unaffected(self):
        """A plain str column without sa_column stays "string"."""
        source = """\
            from typing import Optional
            from sqlmodel import Field, SQLModel

            class User(SQLModel, table=True):
                __tablename__ = "users"
                id: int = Field(primary_key=True)
                name: Optional[str] = Field(default=None)
        """
        result = _parse(source)
        col = next(c for c in result.tables[0].columns if c.name == "name")
        assert col.type == "string"


# ---------------------------------------------------------------------------
# SQLEnum override
# ---------------------------------------------------------------------------


class TestSaColumnSqlEnumOverride:
    def test_sa_column_sqlenum_overrides_type(self):
        """sa_column=Column(SQLEnum(RoleEnum, ...)) → alter type "RoleEnum"."""
        source = """\
            from enum import Enum
            from typing import Optional
            from sqlmodel import Field, SQLModel
            from sqlalchemy import Column
            from sqlalchemy.dialects.postgresql import ENUM as SQLEnum

            class RoleEnum(str, Enum):
                admin = "admin"
                user = "user"

            class Account(SQLModel, table=True):
                __tablename__ = "accounts"
                id: int = Field(primary_key=True)
                role: Optional[str] = Field(
                    default=None,
                    sa_column=Column(SQLEnum(RoleEnum, name="role_enum", schema="app")),
                )
        """
        result = _parse(source)
        col = next(c for c in result.tables[0].columns if c.name == "role")
        assert col.type == "RoleEnum", f"expected 'RoleEnum', got {col.type!r}"

    def test_unknown_enum_in_sa_column_not_overridden(self):
        """SQLEnum with an unknown class name leaves the type unchanged."""
        source = """\
            from typing import Optional
            from sqlmodel import Field, SQLModel
            from sqlalchemy import Column
            from sqlalchemy.dialects.postgresql import ENUM as SQLEnum

            class Thing(SQLModel, table=True):
                __tablename__ = "things"
                id: int = Field(primary_key=True)
                kind: Optional[str] = Field(
                    default=None,
                    sa_column=Column(SQLEnum("UnknownEnum", name="kind")),
                )
        """
        result = _parse(source)
        col = next(c for c in result.tables[0].columns if c.name == "kind")
        # "UnknownEnum" is not a known enum class → type stays as-is
        assert col.type == "string"
