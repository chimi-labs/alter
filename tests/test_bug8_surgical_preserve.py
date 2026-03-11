"""Regression tests — alter apply must not mutate Field() calls spuriously.

ISSUE: ``alter apply`` (surgical update) made three unwanted changes:

  1. ``default={}`` was rewritten to ``default_factory=dict`` (and ``default=[]``
     to ``default_factory=list``) even though both representations are semantically
     equivalent and the file was not otherwise changed.

  2. Field argument order was reshuffled whenever the default-equivalence mismatch
     (issue 1) triggered a rebuild, because ``_rebuild_field_line`` could not find
     ``default`` in the new kwargs dict (which only had ``default_factory``) and
     appended it at the end instead.

  3. Trailing inline comments after Field()'s closing ``)`` were silently dropped
     when ``_rebuild_field_line`` reconstructed the line from scratch.

Fix:
  - ``_normalize_kw_for_eq`` and ``_MUTABLE_DEFAULT_EQUIV`` in ``_surgical.py``
    make the equality check treat ``default={}`` ≡ ``default_factory=dict`` (and
    ``default=[]`` ≡ ``default_factory=list``), preventing unnecessary rebuilds.
  - The merging loop in ``_rebuild_field_line`` now preserves the existing
    representation when an equivalent is found, keeping kwarg order intact.
  - ``_extract_trailing_comment`` captures any ``# …`` suffix after ``)`` and
    re-attaches it to the rebuilt line.
"""

from __future__ import annotations

import textwrap

import pytest

from alter.generators._surgical import (
    _extract_trailing_comment,
    _field_kwargs_equal,
    _normalize_kw_for_eq,
    _rebuild_field_line,
    surgical_update_class,
)


# ---------------------------------------------------------------------------
# Unit tests — _normalize_kw_for_eq
# ---------------------------------------------------------------------------


class TestNormalizeKwForEq:
    def test_default_empty_dict_normalised(self):
        result = _normalize_kw_for_eq({"default": "{}"})
        assert result == {"default_factory": "dict"}

    def test_default_empty_list_normalised(self):
        result = _normalize_kw_for_eq({"default": "[]"})
        assert result == {"default_factory": "list"}

    def test_default_factory_dict_unchanged(self):
        result = _normalize_kw_for_eq({"default_factory": "dict"})
        assert result == {"default_factory": "dict"}

    def test_other_kwargs_unchanged(self):
        kw = {"primary_key": "True", "index": "True"}
        assert _normalize_kw_for_eq(kw) == kw

    def test_nonempty_default_unchanged(self):
        kw = {"default": "None"}
        assert _normalize_kw_for_eq(kw) == kw


# ---------------------------------------------------------------------------
# Unit tests — _field_kwargs_equal (equivalence)
# ---------------------------------------------------------------------------


class TestFieldKwargsEqualEquivalence:
    def test_default_dict_vs_default_factory_dict_equal(self):
        existing = '    meta: Optional[dict] = Field(default={}, nullable=True)'
        new = '    meta: Optional[dict] = Field(default_factory=dict, nullable=True)'
        assert _field_kwargs_equal(existing, new) is True

    def test_default_list_vs_default_factory_list_equal(self):
        existing = '    tags: Optional[list] = Field(default=[], nullable=True)'
        new = '    tags: Optional[list] = Field(default_factory=list, nullable=True)'
        assert _field_kwargs_equal(existing, new) is True

    def test_genuinely_different_kwargs_not_equal(self):
        existing = '    name: str = Field(default="foo")'
        new = '    name: str = Field(default="bar")'
        assert _field_kwargs_equal(existing, new) is False

    def test_same_line_equal(self):
        line = '    role: str = Field(default=None, index=True)'
        assert _field_kwargs_equal(line, line) is True


# ---------------------------------------------------------------------------
# Unit tests — _extract_trailing_comment
# ---------------------------------------------------------------------------


class TestExtractTrailingComment:
    def test_single_line_with_comment(self):
        text = '    role: str = Field(default=None)  # user role'
        assert _extract_trailing_comment(text) == '  # user role'

    def test_single_line_no_comment(self):
        text = '    role: str = Field(default=None)'
        assert _extract_trailing_comment(text) == ''

    def test_multiline_comment_on_closing_paren(self):
        text = textwrap.dedent("""\
            role: str = Field(
                default=None,
            )  # user role""")
        assert _extract_trailing_comment(text) == '  # user role'

    def test_multiline_no_comment(self):
        text = textwrap.dedent("""\
            role: str = Field(
                default=None,
            )""")
        assert _extract_trailing_comment(text) == ''

    def test_comment_with_no_space(self):
        text = '    x: int = Field(default=0)# tight comment'
        result = _extract_trailing_comment(text)
        assert '# tight comment' in result


# ---------------------------------------------------------------------------
# Unit tests — _rebuild_field_line preserves mutable-default representation
# ---------------------------------------------------------------------------


class TestRebuildFieldLineDefaultEquivalence:
    def test_default_dict_kept_when_equivalent(self):
        """When only default={} vs default_factory=dict differs, keep default={}."""
        existing = '    meta: dict = Field(default={}, index=True)'
        new_line = '    meta: dict = Field(default_factory=dict, index=True)'
        rebuilt = _rebuild_field_line(existing, new_line)
        assert 'default={}' in rebuilt
        assert 'default_factory' not in rebuilt

    def test_default_list_kept_when_equivalent(self):
        existing = '    tags: list = Field(default=[], index=True)'
        new_line = '    tags: list = Field(default_factory=list, index=True)'
        rebuilt = _rebuild_field_line(existing, new_line)
        assert 'default=[]' in rebuilt
        assert 'default_factory' not in rebuilt

    def test_kwarg_order_preserved_with_default_dict(self):
        """default={} stays in its original position when a different kwarg changes."""
        existing = '    meta: dict = Field(default={}, foreign_key="other.id", index=False)'
        # Simulate: index changed from False to True
        new_line = '    meta: dict = Field(default_factory=dict, foreign_key="other.id", index=True)'
        rebuilt = _rebuild_field_line(existing, new_line)
        # default should come before foreign_key
        idx_default = rebuilt.index('default={}')
        idx_fk = rebuilt.index('foreign_key')
        assert idx_default < idx_fk
        # The preserved default should still be default={} not default_factory
        assert 'default={}' in rebuilt
        assert 'default_factory' not in rebuilt


# ---------------------------------------------------------------------------
# Unit tests — _rebuild_field_line preserves trailing comments
# ---------------------------------------------------------------------------


class TestRebuildFieldLineTrailingComment:
    def test_single_line_comment_preserved(self):
        existing = '    role: str = Field(default=None)  # user role'
        new_line = '    role: str = Field(default=None, index=True)'
        rebuilt = _rebuild_field_line(existing, new_line)
        assert '# user role' in rebuilt

    def test_multiline_comment_preserved(self):
        existing = textwrap.dedent("""\
                meta: dict = Field(
                    default={},
                    index=True,
                )  # JSON metadata""")
        new_line = '    meta: dict = Field(default_factory=dict, index=True, unique=True)'
        rebuilt = _rebuild_field_line(existing, new_line)
        assert '# JSON metadata' in rebuilt

    def test_no_comment_no_suffix(self):
        existing = '    role: str = Field(default=None)'
        new_line = '    role: str = Field(default=None, index=True)'
        rebuilt = _rebuild_field_line(existing, new_line)
        assert rebuilt.endswith(')')  # no trailing comment added


# ---------------------------------------------------------------------------
# Integration tests — surgical_update_class
# ---------------------------------------------------------------------------


_SOURCE_WITH_DEFAULT_DICT = """\
from typing import Optional
from sqlmodel import SQLModel, Field

class ItemSQL(SQLModel, table=True):
    __tablename__ = "item"
    id: int = Field(primary_key=True)
    meta: Optional[dict] = Field(default={})
    tags: Optional[list] = Field(default=[])
"""


class TestSurgicalUpdateClassNoSpuriousRewrite:
    def test_default_dict_not_rewritten_when_unchanged(self):
        """If schema matches default={}, surgical update must return None (no change)."""
        # The schema line uses default_factory=dict (generator canonical form)
        schema_lines = [
            "    id: int = Field(primary_key=True)",
            "    meta: Optional[dict] = Field(default_factory=dict)",
            "    tags: Optional[list] = Field(default_factory=list)",
        ]
        result = surgical_update_class(_SOURCE_WITH_DEFAULT_DICT, schema_lines)
        assert result is None, (
            "surgical_update_class should return None (no-op) when default={} "
            "is equivalent to default_factory=dict"
        )

    def test_file_unchanged_when_only_equivalence_differs(self):
        """The file content must be preserved byte-for-byte when nothing really changed."""
        from alter.generators.sqlmodel import SQLModelGenerator
        from alter.schema import AlterSchema, Column, Table

        schema = AlterSchema(
            tables=[
                Table(
                    name="item",
                    columns=[
                        Column(name="id", type="int", primary_key=True, nullable=False),
                        Column(name="meta", type="json", nullable=True, default="{}"),
                        Column(name="tags", type="json_array", nullable=True, default="[]"),
                    ],
                )
            ]
        )
        gen = SQLModelGenerator()
        result = gen.update_models(schema, _SOURCE_WITH_DEFAULT_DICT)
        assert result == _SOURCE_WITH_DEFAULT_DICT, (
            "update_models must not rewrite default={} to default_factory=dict\n"
            f"Unexpected diff:\n"
            + "\n".join(
                line
                for line in __import__("difflib").unified_diff(
                    _SOURCE_WITH_DEFAULT_DICT.splitlines(),
                    result.splitlines(),
                    lineterm="",
                )
            )
        )


_SOURCE_WITH_COMMENT = """\
from typing import Optional
from sqlmodel import SQLModel, Field

class UserSQL(SQLModel, table=True):
    __tablename__ = "user"
    id: int = Field(primary_key=True)
    role: str = Field(default="user")  # role must be 'user' or 'admin'
    score: int = Field(default=0)  # points
"""


class TestSurgicalUpdatePreservesComment:
    def test_comment_kept_when_field_unchanged(self):
        """If a field is unchanged, the entire line incl. comment is kept verbatim."""
        schema_lines = [
            "    id: int = Field(primary_key=True)",
            '    role: str = Field(default="user")',
            "    score: int = Field(default=0)",
        ]
        result = surgical_update_class(_SOURCE_WITH_COMMENT, schema_lines)
        # No changes needed → should return None
        assert result is None

    def test_comment_preserved_when_other_kwarg_changes(self):
        """When a *different* kwarg changes, trailing comment must survive the rebuild."""
        schema_lines = [
            "    id: int = Field(primary_key=True)",
            '    role: str = Field(default="user", index=True)',  # index added
            "    score: int = Field(default=0)",
        ]
        result = surgical_update_class(_SOURCE_WITH_COMMENT, schema_lines)
        assert result is not None
        joined = "".join(result)
        assert "# role must be 'user' or 'admin'" in joined
