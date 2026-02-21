"""Round-trip tests: parse → generate → parse → schemas must match.

For each ORM backend:
  1. Parse a source file into an AlterSchema
  2. Generate Python source from that schema
  3. Parse the generated source again
  4. Assert the resulting schemas are equivalent

These tests validate that the parser and generator are inverse operations.
"""

from __future__ import annotations

import tempfile
import os
from pathlib import Path
from textwrap import dedent

import pytest

from alter.parsers.sqlmodel import SQLModelParser
from alter.parsers.sqlalchemy import SQLAlchemyParser
from alter.generators.sqlmodel import SQLModelGenerator
from alter.generators.sqlalchemy import SQLAlchemyGenerator
from alter.schema import AlterSchema, Column, EnumDef, Table


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURE_SQLA = Path(__file__).parent / "fixtures" / "sqlalchemy_models.py"


def _write_tmp(source: str, suffix: str = ".py") -> Path:
    fd, name = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    p = Path(name)
    p.write_text(dedent(source), encoding="utf-8")
    return p


def _schema_from_tables_enums(tables, enums, orm="sqlmodel") -> AlterSchema:
    return AlterSchema(orm=orm, tables=tables, enums=enums)


def _tables_equivalent(a: list[Table], b: list[Table]) -> bool:
    """Check that two table lists have the same tables/columns (ignoring order)."""
    a_map = {t.name: t for t in a}
    b_map = {t.name: t for t in b}
    if set(a_map) != set(b_map):
        return False
    for name, t_a in a_map.items():
        t_b = b_map[name]
        a_cols = {c.name: c for c in t_a.columns}
        b_cols = {c.name: c for c in t_b.columns}
        if set(a_cols) != set(b_cols):
            return False
        for cn, ca in a_cols.items():
            cb = b_cols[cn]
            if ca.type != cb.type:
                return False
            if ca.primary_key != cb.primary_key:
                return False
            if ca.nullable != cb.nullable:
                return False
    return True


def _parse_sqlmodel(source: str) -> tuple[list[Table], list[EnumDef]]:
    tmp = _write_tmp(source)
    try:
        parser = SQLModelParser()
        result = parser._parse_file_internal(tmp)
        return result.tables, result.enums
    finally:
        tmp.unlink()


def _parse_sqlalchemy(source: str) -> tuple[list[Table], list[EnumDef]]:
    tmp = _write_tmp(source)
    try:
        parser = SQLAlchemyParser()
        result = parser._parse_file_internal(tmp)
        return result.tables, result.enums
    finally:
        tmp.unlink()


# ---------------------------------------------------------------------------
# SQLModel round-trips
# ---------------------------------------------------------------------------

def test_sqlmodel_roundtrip_simple_table():
    """Parse → generate → parse: single table with basic columns."""
    source = dedent("""\
        import uuid
        from sqlmodel import Field, SQLModel

        class Widget(SQLModel, table=True):
            __tablename__ = "widgets"
            id: uuid.UUID = Field(primary_key=True, default_factory=uuid.uuid4)
            name: str = Field(max_length=100)
    """)
    tables1, enums1 = _parse_sqlmodel(source)
    schema = _schema_from_tables_enums(tables1, enums1)

    generated = SQLModelGenerator().generate_models(schema)
    tables2, enums2 = _parse_sqlmodel(generated)

    assert {t.name for t in tables1} == {t.name for t in tables2}
    assert _tables_equivalent(tables1, tables2)


def test_sqlmodel_roundtrip_nullable_columns():
    """Optional[T] columns survive the round-trip."""
    source = dedent("""\
        from typing import Optional
        from sqlmodel import Field, SQLModel

        class Post(SQLModel, table=True):
            __tablename__ = "posts"
            id: int = Field(primary_key=True)
            body: Optional[str] = Field(default=None)
    """)
    tables1, enums1 = _parse_sqlmodel(source)
    schema = _schema_from_tables_enums(tables1, enums1)

    generated = SQLModelGenerator().generate_models(schema)
    tables2, enums2 = _parse_sqlmodel(generated)

    assert _tables_equivalent(tables1, tables2)
    post2 = next(t for t in tables2 if t.name == "posts")
    body = next(c for c in post2.columns if c.name == "body")
    assert body.nullable is True
    assert body.type == "string"


def test_sqlmodel_roundtrip_enum_column():
    """Enum definitions and enum-typed columns survive the round-trip."""
    source = dedent("""\
        from enum import Enum
        from sqlmodel import Field, SQLModel

        class Status(str, Enum):
            active = "active"
            inactive = "inactive"

        class Account(SQLModel, table=True):
            __tablename__ = "accounts"
            id: int = Field(primary_key=True)
            status: Status = Field(default=Status.active)
    """)
    tables1, enums1 = _parse_sqlmodel(source)
    schema = _schema_from_tables_enums(tables1, enums1)

    generated = SQLModelGenerator().generate_models(schema)
    tables2, enums2 = _parse_sqlmodel(generated)

    assert {e.name for e in enums1} == {e.name for e in enums2}
    assert _tables_equivalent(tables1, tables2)


def test_sqlmodel_roundtrip_foreign_key():
    """FK columns survive the round-trip with correct reference."""
    source = dedent("""\
        import uuid
        from sqlmodel import Field, SQLModel

        class User(SQLModel, table=True):
            __tablename__ = "users"
            id: uuid.UUID = Field(primary_key=True, default_factory=uuid.uuid4)

        class Post(SQLModel, table=True):
            __tablename__ = "posts"
            id: uuid.UUID = Field(primary_key=True, default_factory=uuid.uuid4)
            user_id: uuid.UUID = Field(foreign_key="users.id")
    """)
    tables1, enums1 = _parse_sqlmodel(source)
    schema = _schema_from_tables_enums(tables1, enums1)

    generated = SQLModelGenerator().generate_models(schema)
    tables2, enums2 = _parse_sqlmodel(generated)

    assert _tables_equivalent(tables1, tables2)
    post2 = next(t for t in tables2 if t.name == "posts")
    uid = next(c for c in post2.columns if c.name == "user_id")
    assert uid.foreign_key == "users.id"


def test_sqlmodel_roundtrip_saas_starter():
    """Full SaaS starter fixture survives parse → generate → parse.

    Models are now split across app/enums.py, app/models/parents.py and
    app/models/starter.py — use parse_directory to pick them all up.
    """
    saas_app = Path(__file__).parent.parent / "examples" / "saas-starter" / "app"
    saas_root = saas_app.parent
    parser = SQLModelParser(project_root=saas_root)
    dir_result = parser.parse_directory(saas_app)
    schema = dir_result.schema

    generated = SQLModelGenerator().generate_models(schema)
    tables2, enums2 = _parse_sqlmodel(generated)

    # All original table names must survive
    orig_names = {t.name for t in schema.tables}
    gen_names = {t.name for t in tables2}
    assert orig_names == gen_names

    # All original enum names must survive
    assert {e.name for e in schema.enums} == {e.name for e in enums2}


# ---------------------------------------------------------------------------
# SQLAlchemy round-trips
# ---------------------------------------------------------------------------

def test_sqlalchemy_roundtrip_simple_table():
    """SQLAlchemy parse → generate → parse: single table."""
    tables1, enums1 = _parse_sqlalchemy(FIXTURE_SQLA.read_text())
    schema = _schema_from_tables_enums(tables1, enums1, orm="sqlalchemy")

    generated = SQLAlchemyGenerator().generate_models(schema)
    tables2, enums2 = _parse_sqlalchemy(generated)

    orig_names = {t.name for t in tables1}
    gen_names = {t.name for t in tables2}
    assert orig_names == gen_names


def test_sqlalchemy_roundtrip_nullable_columns():
    """Optional / nullable columns survive SQLAlchemy round-trip."""
    source = dedent("""\
        from typing import Optional
        from sqlalchemy import String
        from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

        class Base(DeclarativeBase):
            pass

        class Note(Base):
            __tablename__ = "notes"
            id: Mapped[int] = mapped_column(primary_key=True, nullable=False)
            body: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    """)
    tables1, enums1 = _parse_sqlalchemy(source)
    schema = _schema_from_tables_enums(tables1, enums1, orm="sqlalchemy")

    generated = SQLAlchemyGenerator().generate_models(schema)
    tables2, enums2 = _parse_sqlalchemy(generated)

    assert _tables_equivalent(tables1, tables2)
    note2 = next(t for t in tables2 if t.name == "notes")
    body = next(c for c in note2.columns if c.name == "body")
    assert body.nullable is True


def test_sqlalchemy_roundtrip_foreign_key():
    """FK columns survive SQLAlchemy round-trip."""
    source = dedent("""\
        from sqlalchemy import ForeignKey, Integer
        from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

        class Base(DeclarativeBase):
            pass

        class Comment(Base):
            __tablename__ = "comments"
            id: Mapped[int] = mapped_column(Integer, primary_key=True, nullable=False)
            post_id: Mapped[int] = mapped_column(Integer, ForeignKey("posts.id"),
                                                 nullable=False)
    """)
    tables1, enums1 = _parse_sqlalchemy(source)
    schema = _schema_from_tables_enums(tables1, enums1, orm="sqlalchemy")

    generated = SQLAlchemyGenerator().generate_models(schema)
    tables2, enums2 = _parse_sqlalchemy(generated)

    assert _tables_equivalent(tables1, tables2)
    c2 = next(t for t in tables2 if t.name == "comments")
    pid = next(c for c in c2.columns if c.name == "post_id")
    assert pid.foreign_key == "posts.id"
