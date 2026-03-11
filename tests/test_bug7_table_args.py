"""Regression tests for Fix 7 — __table_args__ schema preservation.

ISSUE: ``alter apply`` did not preserve ``__table_args__ = {"schema": "..."}``
when a SQLModel/SQLAlchemy model used a PostgreSQL schema name.

Fix:
- ``Table.schema_name`` field added to the .alter schema.
- SQLModel and SQLAlchemy parsers extract the schema from ``__table_args__``.
- SQLModel generator emits ``__table_args__`` when ``schema_name`` is set.
- Surgical update path already preserved the line verbatim (non-field line);
  this fix ensures full regeneration also emits it.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from textwrap import dedent

import pytest

from alter.generators.sqlmodel import SQLModelGenerator, _model_class_source
from alter.parsers.sqlalchemy import SQLAlchemyParser
from alter.parsers.sqlmodel import SQLModelParser, _get_table_schema
from alter.schema import AlterSchema, Column, Table
import ast


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_sqlmodel_source(source: str):
    fd, name = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    p = Path(name)
    p.write_text(dedent(source))
    try:
        parser = SQLModelParser()
        return parser._parse_file_internal(p)
    finally:
        p.unlink()


def _parse_sqla_source(source: str):
    fd, name = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    p = Path(name)
    p.write_text(dedent(source))
    try:
        parser = SQLAlchemyParser()
        return parser._parse_file_internal(p)
    finally:
        p.unlink()


def _update_source(source: str, schema: AlterSchema) -> str:
    return SQLModelGenerator().update_models(schema, dedent(source))


# ---------------------------------------------------------------------------
# Unit test: _get_table_schema AST helper (SQLModel parser)
# ---------------------------------------------------------------------------


class TestGetTableSchemaHelper:
    def test_extracts_schema_from_simple_dict(self):
        src = dedent("""\
            class User(SQLModel, table=True):
                __tablename__ = "users"
                __table_args__ = {"schema": "myschema"}
                id: int = Field(primary_key=True)
        """)
        tree = ast.parse(src)
        cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
        assert _get_table_schema(cls) == "myschema"

    def test_extracts_schema_from_mixed_dict(self):
        """__table_args__ with additional keys still yields the schema value."""
        src = dedent("""\
            class User(SQLModel, table=True):
                __tablename__ = "users"
                __table_args__ = {"schema": "analytics", "mysql_engine": "InnoDB"}
                id: int = Field(primary_key=True)
        """)
        tree = ast.parse(src)
        cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
        assert _get_table_schema(cls) == "analytics"

    def test_returns_none_when_no_table_args(self):
        src = dedent("""\
            class User(SQLModel, table=True):
                __tablename__ = "users"
                id: int = Field(primary_key=True)
        """)
        tree = ast.parse(src)
        cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
        assert _get_table_schema(cls) is None

    def test_returns_none_when_table_args_has_no_schema_key(self):
        src = dedent("""\
            class User(SQLModel, table=True):
                __tablename__ = "users"
                __table_args__ = {"mysql_engine": "InnoDB"}
                id: int = Field(primary_key=True)
        """)
        tree = ast.parse(src)
        cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
        assert _get_table_schema(cls) is None


# ---------------------------------------------------------------------------
# Parser: SQLModel — schema_name round-trips through Table
# ---------------------------------------------------------------------------


class TestSQLModelParserSchemaName:
    def test_parser_extracts_schema_name(self):
        source = """\
            from typing import Optional
            from sqlmodel import Field, SQLModel

            class Session(SQLModel, table=True):
                __tablename__ = "sessions"
                __table_args__ = {"schema": "myschema"}

                id: int = Field(primary_key=True)
                token: str = Field()
        """
        result = _parse_sqlmodel_source(source)
        assert len(result.tables) == 1
        table = result.tables[0]
        assert table.schema_name == "myschema"

    def test_parser_sets_schema_name_none_when_absent(self):
        source = """\
            from sqlmodel import Field, SQLModel

            class User(SQLModel, table=True):
                __tablename__ = "users"
                id: int = Field(primary_key=True)
        """
        result = _parse_sqlmodel_source(source)
        assert result.tables[0].schema_name is None

    def test_parser_schema_name_with_qualified_fk(self):
        """schema_name and schema-qualified FK can coexist."""
        source = """\
            from typing import Optional
            from sqlmodel import Field, SQLModel

            class Message(SQLModel, table=True):
                __tablename__ = "messages"
                __table_args__ = {"schema": "chat"}

                id: int = Field(primary_key=True)
                user_id: int = Field(foreign_key="auth.users.id")
        """
        result = _parse_sqlmodel_source(source)
        table = result.tables[0]
        assert table.schema_name == "chat"
        id_col = next(c for c in table.columns if c.name == "user_id")
        assert id_col.foreign_key == "auth.users.id"


# ---------------------------------------------------------------------------
# Parser: SQLAlchemy — schema_name round-trips through Table
# ---------------------------------------------------------------------------


class TestSQLAlchemyParserSchemaName:
    def test_parser_extracts_schema_name_20_style(self):
        source = """\
            from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

            class Base(DeclarativeBase):
                pass

            class Order(Base):
                __tablename__ = "orders"
                __table_args__ = {"schema": "billing"}

                id: Mapped[int] = mapped_column(primary_key=True)
        """
        result = _parse_sqla_source(source)
        assert len(result.tables) == 1
        assert result.tables[0].schema_name == "billing"

    def test_parser_sets_schema_name_none_when_absent(self):
        source = """\
            from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

            class Base(DeclarativeBase):
                pass

            class Product(Base):
                __tablename__ = "products"
                id: Mapped[int] = mapped_column(primary_key=True)
        """
        result = _parse_sqla_source(source)
        assert result.tables[0].schema_name is None


# ---------------------------------------------------------------------------
# Generator: _model_class_source emits __table_args__
# ---------------------------------------------------------------------------


class TestGeneratorEmitsTableArgs:
    def test_schema_name_emits_table_args(self):
        table = Table(
            name="sessions",
            schema_name="myschema",
            columns=[Column(name="id", type="int", primary_key=True, nullable=False)],
        )
        src = _model_class_source(table, set())
        assert '__table_args__ = {"schema": "myschema"}' in src

    def test_no_schema_name_no_table_args(self):
        table = Table(
            name="users",
            columns=[Column(name="id", type="int", primary_key=True, nullable=False)],
        )
        src = _model_class_source(table, set())
        assert "__table_args__" not in src

    def test_table_args_appears_after_tablename(self):
        table = Table(
            name="events",
            schema_name="analytics",
            columns=[Column(name="id", type="int", primary_key=True, nullable=False)],
        )
        src = _model_class_source(table, set())
        tablename_pos = src.index("__tablename__")
        table_args_pos = src.index("__table_args__")
        assert table_args_pos > tablename_pos, "__table_args__ should come after __tablename__"


# ---------------------------------------------------------------------------
# Full round-trip: parse → schema → generate_models
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_generate_models_round_trips_schema_name(self):
        """generate_models (full regeneration) preserves schema_name."""
        source = """\
            from sqlmodel import Field, SQLModel

            class Log(SQLModel, table=True):
                __tablename__ = "logs"
                __table_args__ = {"schema": "audit"}

                id: int = Field(primary_key=True)
                message: str = Field()
        """
        result = _parse_sqlmodel_source(source)
        table = result.tables[0]
        assert table.schema_name == "audit"

        schema = AlterSchema(orm="sqlmodel", tables=[table])
        generated = SQLModelGenerator().generate_models(schema)
        assert '__table_args__ = {"schema": "audit"}' in generated

    def test_update_models_preserves_schema_name_when_fields_change(self):
        """Surgical update preserves __table_args__ when a field is added."""
        original = dedent("""\
            from sqlmodel import Field, SQLModel

            class Item(SQLModel, table=True):
                __tablename__ = "items"
                __table_args__ = {"schema": "store"}

                id: int = Field(primary_key=True)
        """)
        table = Table(
            name="items",
            schema_name="store",
            columns=[
                Column(name="id", type="int", primary_key=True, nullable=False),
                Column(name="name", type="string", nullable=False),
            ],
        )
        schema = AlterSchema(orm="sqlmodel", tables=[table])
        updated = _update_source(original, schema)
        assert '__table_args__ = {"schema": "store"}' in updated
        # The new column should also be present
        assert "name: str" in updated

    def test_update_models_preserves_schema_name_when_unchanged(self):
        """When no fields change, the file is returned untouched (includes __table_args__)."""
        original = dedent("""\
            from sqlmodel import Field, SQLModel

            class Widget(SQLModel, table=True):
                __tablename__ = "widgets"
                __table_args__ = {"schema": "factory"}

                id: int = Field(primary_key=True)
        """)
        table = Table(
            name="widgets",
            schema_name="factory",
            columns=[
                Column(name="id", type="int", primary_key=True, nullable=False),
            ],
        )
        schema = AlterSchema(orm="sqlmodel", tables=[table])
        updated = _update_source(original, schema)
        assert '__table_args__ = {"schema": "factory"}' in updated
