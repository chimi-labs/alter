"""Regression tests — __table_args__ tuple form schema extraction.

ISSUE: schema_name was not extracted when ``__table_args__`` used the tuple
form required by SQLAlchemy/SQLModel when combining Index/UniqueConstraint
objects with table-level keyword options.

  # Was broken (schema_name=None):
  __table_args__ = (Index("ix_foo", "col_a"), {"schema": "myschema"})

  # Was already working:
  __table_args__ = {"schema": "myschema"}

Fix: ``_get_table_schema`` in both parsers now walks the tuple in reverse and
uses the first ``ast.Dict`` element it finds as the options dict.
"""

from __future__ import annotations

import ast

import pytest

from alter.parsers.sqlalchemy import SQLAlchemyParser
from alter.parsers.sqlmodel import SQLModelParser, _get_table_schema


# ---------------------------------------------------------------------------
# Low-level unit tests for _get_table_schema directly
# ---------------------------------------------------------------------------


def _class_def(source: str) -> ast.ClassDef:
    """Parse *source* and return the first ClassDef node."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            return node
    raise ValueError("No ClassDef found in source")


class TestGetTableSchemaDirectly:
    """Unit tests that call _get_table_schema (SQLModel version) directly."""

    def test_plain_dict_form(self):
        src = """
class Foo:
    __table_args__ = {"schema": "myschema"}
"""
        assert _get_table_schema(_class_def(src)) == "myschema"

    def test_tuple_one_index(self):
        src = """
class Foo:
    __table_args__ = (Index("ix_foo", "col_a"), {"schema": "myschema"})
"""
        assert _get_table_schema(_class_def(src)) == "myschema"

    def test_tuple_multiple_indexes(self):
        src = """
class Foo:
    __table_args__ = (
        Index("ix_foo", "col_a"),
        Index("ix_bar", "col_b"),
        {"schema": "myschema"},
    )
"""
        assert _get_table_schema(_class_def(src)) == "myschema"

    def test_tuple_unique_constraint(self):
        src = """
class Foo:
    __table_args__ = (
        UniqueConstraint("email", name="uq_email"),
        {"schema": "billing"},
    )
"""
        assert _get_table_schema(_class_def(src)) == "billing"

    def test_tuple_no_schema_key_returns_none(self):
        """Tuple present but no "schema" key → None."""
        src = """
class Foo:
    __table_args__ = (Index("ix_foo", "col_a"), {"extend_existing": True})
"""
        assert _get_table_schema(_class_def(src)) is None

    def test_tuple_empty_dict_returns_none(self):
        src = """
class Foo:
    __table_args__ = (Index("ix_foo", "col_a"), {})
"""
        assert _get_table_schema(_class_def(src)) is None

    def test_no_table_args_returns_none(self):
        src = """
class Foo:
    id: int = Field(primary_key=True)
"""
        assert _get_table_schema(_class_def(src)) is None

    def test_plain_dict_with_extra_keys(self):
        """schema key among other keys in a plain dict."""
        src = """
class Foo:
    __table_args__ = {"extend_existing": True, "schema": "analytics"}
"""
        assert _get_table_schema(_class_def(src)) == "analytics"


# ---------------------------------------------------------------------------
# Integration tests — full parser round-trip
# ---------------------------------------------------------------------------


SQLMODEL_PLAIN_DICT = """\
from sqlmodel import SQLModel, Field

class ItemSQL(SQLModel, table=True):
    __tablename__ = "item"
    __table_args__ = {"schema": "shop"}
    id: int = Field(primary_key=True)
    name: str
"""

SQLMODEL_TUPLE_ONE_INDEX = """\
from sqlmodel import SQLModel, Field
from sqlalchemy import Index

class ItemSQL(SQLModel, table=True):
    __tablename__ = "item"
    __table_args__ = (Index("ix_item_name", "name"), {"schema": "shop"})
    id: int = Field(primary_key=True)
    name: str
"""

SQLMODEL_TUPLE_MULTI_INDEX = """\
from sqlmodel import SQLModel, Field
from sqlalchemy import Index

class ItemSQL(SQLModel, table=True):
    __tablename__ = "item"
    __table_args__ = (
        Index("ix_item_name", "name"),
        Index("ix_item_id_name", "id", "name"),
        {"schema": "shop"},
    )
    id: int = Field(primary_key=True)
    name: str
"""

SQLALCHEMY_TUPLE_ONE_INDEX = """\
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import Index, String

class Base(DeclarativeBase):
    pass

class OrderSQL(Base):
    __tablename__ = "order"
    __table_args__ = (Index("ix_order_ref", "ref"), {"schema": "sales"})
    id: Mapped[int] = mapped_column(primary_key=True)
    ref: Mapped[str] = mapped_column(String(64))
"""


def _parse_sqlmodel(source: str):
    import os, tempfile
    from pathlib import Path
    from textwrap import dedent

    fd, name = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    p = Path(name)
    p.write_text(dedent(source))
    try:
        parser = SQLModelParser()
        schema = parser.parse_file(p)
        return schema
    finally:
        p.unlink()


def _parse_sqlalchemy(source: str):
    import os, tempfile
    from pathlib import Path
    from textwrap import dedent

    fd, name = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    p = Path(name)
    p.write_text(dedent(source))
    try:
        parser = SQLAlchemyParser()
        schema = parser.parse_file(p)
        return schema
    finally:
        p.unlink()


class TestSQLModelParserTupleTableArgs:
    def test_plain_dict_schema_extracted(self):
        tables = _parse_sqlmodel(SQLMODEL_PLAIN_DICT)
        assert len(tables) == 1
        assert tables[0].schema_name == "shop"

    def test_tuple_one_index_schema_extracted(self):
        tables = _parse_sqlmodel(SQLMODEL_TUPLE_ONE_INDEX)
        assert len(tables) == 1
        assert tables[0].schema_name == "shop"

    def test_tuple_multi_index_schema_extracted(self):
        tables = _parse_sqlmodel(SQLMODEL_TUPLE_MULTI_INDEX)
        assert len(tables) == 1
        assert tables[0].schema_name == "shop"

    def test_columns_still_parsed_correctly(self):
        """Ensure column parsing is not disrupted by tuple __table_args__."""
        tables = _parse_sqlmodel(SQLMODEL_TUPLE_ONE_INDEX)
        table = tables[0]
        col_names = [c.name for c in table.columns]
        assert "id" in col_names
        assert "name" in col_names


class TestSQLAlchemyParserTupleTableArgs:
    def test_tuple_one_index_schema_extracted(self):
        tables = _parse_sqlalchemy(SQLALCHEMY_TUPLE_ONE_INDEX)
        order_tables = [t for t in tables if t.name == "order"]
        assert len(order_tables) == 1
        assert order_tables[0].schema_name == "sales"
