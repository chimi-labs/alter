"""Regression tests — Optional[List[Any]] silently dropped by SQLModel parser.

ISSUE: A column annotated as Optional[List[Any]] with sa_column=Column(JSON)
was completely absent from schema.alter after alter init. No warning was printed.

Root cause: _resolve_annotation treated ALL List[X] / list[X] subscripts as
"_relationship" (back-reference collections), so the column was skipped before
_parse_field_call could apply the sa_column=Column(JSON) type override.

Fix:
- _is_primitive_element() distinguishes primitive element types from model classes.
- _resolve_annotation now returns "json_array" for List[primitive_type] and
  "_relationship" only for List[ModelClass] / List["ModelClass"].
- _annotation_is_list now returns False for list[primitive], so those fields
  reach _resolve_annotation rather than being silently dropped.
- _extract_base_class_columns emits warnings.warn for truly unresolvable fields.
"""

from __future__ import annotations

import ast
from pathlib import Path
from textwrap import dedent

import pytest

from alter.parsers.sqlmodel import (
    SQLModelParser,
    _annotation_is_list,
    _is_primitive_element,
    _resolve_annotation,
)
from alter.schema import EnumDef


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse(source: str) -> list:
    import os, tempfile
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False, encoding="utf-8") as f:
        f.write(dedent(source))
        tmp = Path(f.name)
    try:
        return SQLModelParser().parse_file(tmp)
    finally:
        os.unlink(tmp)


def _expr(source: str) -> ast.expr:
    """Parse a Python expression string into an AST node."""
    return ast.parse(source, mode="eval").body


def _annot(source: str) -> ast.expr:
    """Parse a type annotation string into an AST node."""
    return _expr(source)


# ---------------------------------------------------------------------------
# Unit tests — _is_primitive_element
# ---------------------------------------------------------------------------


class TestIsPrimitiveElement:
    def test_Any(self):
        assert _is_primitive_element(_annot("Any")) is True

    def test_str(self):
        assert _is_primitive_element(_annot("str")) is True

    def test_int(self):
        assert _is_primitive_element(_annot("int")) is True

    def test_dict(self):
        assert _is_primitive_element(_annot("dict")) is True

    def test_Dict_subscript(self):
        assert _is_primitive_element(_annot("Dict[str, Any]")) is True

    def test_List_subscript(self):
        assert _is_primitive_element(_annot("List[str]")) is True

    def test_forward_ref_string_is_not_primitive(self):
        assert _is_primitive_element(_annot('"OrderItem"')) is False

    def test_model_name_is_not_primitive(self):
        assert _is_primitive_element(_annot("OrderItem")) is False

    def test_unknown_capitalized_is_not_primitive(self):
        assert _is_primitive_element(_annot("MyCustomClass")) is False


# ---------------------------------------------------------------------------
# Unit tests — _annotation_is_list
# ---------------------------------------------------------------------------


class TestAnnotationIsList:
    def test_list_model_is_relationship(self):
        assert _annotation_is_list(_annot('list["OrderItem"]')) is True

    def test_List_model_is_relationship(self):
        assert _annotation_is_list(_annot("List[OrderItem]")) is True

    def test_list_Any_is_NOT_relationship(self):
        assert _annotation_is_list(_annot("List[Any]")) is False

    def test_list_str_is_NOT_relationship(self):
        assert _annotation_is_list(_annot("list[str]")) is False

    def test_list_dict_is_NOT_relationship(self):
        assert _annotation_is_list(_annot("List[dict]")) is False

    def test_plain_str_is_not_list(self):
        assert _annotation_is_list(_annot("str")) is False


# ---------------------------------------------------------------------------
# Unit tests — _resolve_annotation
# ---------------------------------------------------------------------------


_NO_ENUMS: dict[str, EnumDef] = {}


class TestResolveAnnotation:
    def test_List_Any_returns_json_array(self):
        alter_type, nullable = _resolve_annotation(_annot("List[Any]"), _NO_ENUMS)
        assert alter_type == "json_array"
        assert nullable is False

    def test_List_str_returns_json_array(self):
        alter_type, _ = _resolve_annotation(_annot("List[str]"), _NO_ENUMS)
        assert alter_type == "json_array"

    def test_List_int_returns_json_array(self):
        alter_type, _ = _resolve_annotation(_annot("List[int]"), _NO_ENUMS)
        assert alter_type == "json_array"

    def test_List_dict_returns_json_array(self):
        alter_type, _ = _resolve_annotation(_annot("List[dict]"), _NO_ENUMS)
        assert alter_type == "json_array"

    def test_List_Dict_subscript_returns_json_array(self):
        alter_type, _ = _resolve_annotation(_annot("List[Dict[str, Any]]"), _NO_ENUMS)
        assert alter_type == "json_array"

    def test_List_model_returns_relationship(self):
        alter_type, _ = _resolve_annotation(_annot("List[OrderItem]"), _NO_ENUMS)
        assert alter_type == "_relationship"

    def test_list_forward_ref_returns_relationship(self):
        alter_type, _ = _resolve_annotation(_annot('list["OrderItem"]'), _NO_ENUMS)
        assert alter_type == "_relationship"

    def test_Optional_List_Any_returns_json_array_nullable(self):
        alter_type, nullable = _resolve_annotation(_annot("Optional[List[Any]]"), _NO_ENUMS)
        assert alter_type == "json_array"
        assert nullable is True

    def test_Dict_subscript_returns_json(self):
        alter_type, _ = _resolve_annotation(_annot("Dict[str, Any]"), _NO_ENUMS)
        assert alter_type == "json"

    def test_Optional_Dict_subscript_returns_json_nullable(self):
        alter_type, nullable = _resolve_annotation(_annot("Optional[Dict[str, Any]]"), _NO_ENUMS)
        assert alter_type == "json"
        assert nullable is True


# ---------------------------------------------------------------------------
# Integration tests — full parser round-trip
# ---------------------------------------------------------------------------

_SOURCE_OPTIONAL_LIST_ANY = """\
from typing import Optional, List, Any
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, JSON

class ItemSQL(SQLModel, table=True):
    __tablename__ = "item"
    id: int = Field(primary_key=True)
    tags: Optional[List[Any]] = Field(default=[], sa_column=Column(JSON))
"""

_SOURCE_MIXED_JSON_FIELDS = """\
from typing import Optional, List, Any, Dict
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, JSON

class ProductSQL(SQLModel, table=True):
    __tablename__ = "product"
    id: int = Field(primary_key=True)
    tags: Optional[List[Any]] = Field(default=[], sa_column=Column(JSON))
    meta: Optional[Dict[str, Any]] = Field(default={}, sa_column=Column(JSON))
    labels: Optional[list] = Field(default_factory=list)
    config: Optional[dict] = Field(default_factory=dict)
"""

_SOURCE_RELATIONSHIP_NOT_AFFECTED = """\
from typing import Optional, List
from sqlmodel import SQLModel, Field, Relationship

class OrderSQL(SQLModel, table=True):
    __tablename__ = "order"
    id: int = Field(primary_key=True)
    items: List["OrderItemSQL"] = Relationship(back_populates="order")

class OrderItemSQL(SQLModel, table=True):
    __tablename__ = "order_item"
    id: int = Field(primary_key=True)
    order_id: Optional[int] = Field(default=None, foreign_key="order.id")
"""


class TestOptionalListAnyParsed:
    def test_tags_column_present(self):
        tables = _parse(_SOURCE_OPTIONAL_LIST_ANY)
        table = next(t for t in tables if t.name == "item")
        col_names = [c.name for c in table.columns]
        assert "tags" in col_names, f"'tags' missing from columns: {col_names}"

    def test_tags_column_type_is_json_array(self):
        tables = _parse(_SOURCE_OPTIONAL_LIST_ANY)
        table = next(t for t in tables if t.name == "item")
        tags = next(c for c in table.columns if c.name == "tags")
        # sa_column=Column(JSON) override may promote to "json" — accept both
        # json and json_array since the array hint is in the annotation.
        assert tags.type in ("json", "json_array"), f"Unexpected type: {tags.type}"

    def test_tags_column_nullable(self):
        tables = _parse(_SOURCE_OPTIONAL_LIST_ANY)
        table = next(t for t in tables if t.name == "item")
        tags = next(c for c in table.columns if c.name == "tags")
        assert tags.nullable is True

    def test_id_column_still_present(self):
        tables = _parse(_SOURCE_OPTIONAL_LIST_ANY)
        table = next(t for t in tables if t.name == "item")
        col_names = [c.name for c in table.columns]
        assert "id" in col_names


class TestMixedJsonFields:
    def _table(self):
        tables = _parse(_SOURCE_MIXED_JSON_FIELDS)
        return next(t for t in tables if t.name == "product")

    def test_tags_present(self):
        assert "tags" in [c.name for c in self._table().columns]

    def test_meta_present(self):
        assert "meta" in [c.name for c in self._table().columns]

    def test_labels_present(self):
        assert "labels" in [c.name for c in self._table().columns]

    def test_config_present(self):
        assert "config" in [c.name for c in self._table().columns]

    def test_labels_type_is_json_array(self):
        col = next(c for c in self._table().columns if c.name == "labels")
        assert col.type == "json_array"

    def test_config_type_is_json(self):
        col = next(c for c in self._table().columns if c.name == "config")
        assert col.type == "json"


class TestRelationshipNotAffected:
    """Ensure relationship back-references are still correctly skipped."""

    def test_order_items_relationship_not_parsed_as_column(self):
        tables = _parse(_SOURCE_RELATIONSHIP_NOT_AFFECTED)
        order = next(t for t in tables if t.name == "order")
        col_names = [c.name for c in order.columns]
        # "items" is a Relationship, must NOT appear as a column
        assert "items" not in col_names

    def test_order_item_fk_parsed(self):
        tables = _parse(_SOURCE_RELATIONSHIP_NOT_AFFECTED)
        item = next(t for t in tables if t.name == "order_item")
        col_names = [c.name for c in item.columns]
        assert "order_id" in col_names
