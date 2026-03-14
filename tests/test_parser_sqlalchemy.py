"""Tests for the SQLAlchemy parser backend.

Uses the fixture at ``tests/fixtures/sqlalchemy_models.py`` as the parsing
target. No user code is imported or executed.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from alter.parsers.sqlalchemy import SQLAlchemyParser
from alter.parsers import detect_project_orm
from alter.schema import AlterSchema

# Fixture path
FIXTURE = Path(__file__).parent / "fixtures" / "sqlalchemy_models.py"
FIXTURES_DIR = FIXTURE.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse_source(source: str, project_root: Path | None = None):
    """Write source to a tmp file and parse it, returning list[Table]."""
    import tempfile, os
    with tempfile.NamedTemporaryFile(
        suffix=".py", mode="w", delete=False, encoding="utf-8"
    ) as f:
        f.write(dedent(source))
        tmp = Path(f.name)
    try:
        parser = SQLAlchemyParser(project_root=project_root)
        return parser.parse_file(tmp)
    finally:
        os.unlink(tmp)


def parse_source_full(source: str):
    """Return _FileResult for richer assertions."""
    import tempfile, os
    with tempfile.NamedTemporaryFile(
        suffix=".py", mode="w", delete=False, encoding="utf-8"
    ) as f:
        f.write(dedent(source))
        tmp = Path(f.name)
    try:
        parser = SQLAlchemyParser()
        return parser._parse_file_internal(tmp)
    finally:
        os.unlink(tmp)


# ---------------------------------------------------------------------------
# 1. ORM detection
# ---------------------------------------------------------------------------


def test_detect_orm_sqlalchemy_file(tmp_path: Path) -> None:
    f = tmp_path / "models.py"
    f.write_text("from sqlalchemy.orm import DeclarativeBase\n")
    assert SQLAlchemyParser().detect_orm(f) is True


def test_detect_orm_sqlalchemy_column_import(tmp_path: Path) -> None:
    f = tmp_path / "models.py"
    f.write_text("from sqlalchemy import Column\n")
    assert SQLAlchemyParser().detect_orm(f) is True


def test_detect_orm_non_sqlalchemy_file(tmp_path: Path) -> None:
    f = tmp_path / "models.py"
    f.write_text("import os\n")
    assert SQLAlchemyParser().detect_orm(f) is False


# ---------------------------------------------------------------------------
# 2. Fixture — overall structure
# ---------------------------------------------------------------------------


def test_fixture_parses_four_tables() -> None:
    parser = SQLAlchemyParser(project_root=FIXTURES_DIR)
    tables = parser.parse_file(FIXTURE)
    names = {t.name for t in tables}
    assert "teams" in names
    assert "members" in names
    assert "articles" in names
    assert "tags" in names


def test_fixture_no_base_class_in_tables() -> None:
    """The Base(DeclarativeBase) class must not appear as a table."""
    parser = SQLAlchemyParser()
    tables = parser.parse_file(FIXTURE)
    names = {t.name for t in tables}
    assert "base" not in names


def test_fixture_enums_detected() -> None:
    parser = SQLAlchemyParser()
    result = parser._parse_file_internal(FIXTURE)
    enum_names = {e.name for e in result.enums}
    assert "TeamRole" in enum_names
    assert "ArticleStatus" in enum_names


# ---------------------------------------------------------------------------
# 3. 2.0 style — Mapped[T] + mapped_column()
# ---------------------------------------------------------------------------


def test_mapped_column_primary_key() -> None:
    parser = SQLAlchemyParser()
    tables = parser.parse_file(FIXTURE)
    team = next(t for t in tables if t.name == "teams")
    pk = next(c for c in team.columns if c.primary_key)
    assert pk.name == "id"
    assert pk.type == "uuid"
    assert pk.nullable is False


def test_mapped_column_unique_and_index() -> None:
    parser = SQLAlchemyParser()
    tables = parser.parse_file(FIXTURE)
    team = next(t for t in tables if t.name == "teams")
    slug = next(c for c in team.columns if c.name == "slug")
    assert slug.unique is True
    assert slug.index is True


def test_mapped_column_optional_nullable() -> None:
    parser = SQLAlchemyParser()
    tables = parser.parse_file(FIXTURE)
    member = next(t for t in tables if t.name == "members")
    bio = next(c for c in member.columns if c.name == "bio")
    assert bio.nullable is True
    assert bio.type == "string"


def test_mapped_column_foreign_key() -> None:
    parser = SQLAlchemyParser()
    result = parser._parse_file_internal(FIXTURE)
    member_rels = [r for r in result.relations if r.from_table == "members"]
    assert len(member_rels) >= 1
    team_rel = next(r for r in member_rels if r.to_table == "teams")
    assert team_rel.from_column == "team_id"
    assert team_rel.to_column == "id"


def test_mapped_column_relationship_skipped() -> None:
    """relationship() fields must not appear as columns."""
    parser = SQLAlchemyParser()
    tables = parser.parse_file(FIXTURE)
    team = next(t for t in tables if t.name == "teams")
    col_names = {c.name for c in team.columns}
    assert "members" not in col_names


def test_mapped_string_type() -> None:
    parser = SQLAlchemyParser()
    tables = parser.parse_file(FIXTURE)
    team = next(t for t in tables if t.name == "teams")
    name_col = next(c for c in team.columns if c.name == "name")
    assert name_col.type == "string"


def test_mapped_datetime_type() -> None:
    parser = SQLAlchemyParser()
    tables = parser.parse_file(FIXTURE)
    team = next(t for t in tables if t.name == "teams")
    ts = next(c for c in team.columns if c.name == "created_at")
    assert ts.type == "datetime"


# ---------------------------------------------------------------------------
# 4. 1.x style — Column(Type, ...)
# ---------------------------------------------------------------------------


def test_legacy_column_primary_key() -> None:
    parser = SQLAlchemyParser()
    tables = parser.parse_file(FIXTURE)
    article = next(t for t in tables if t.name == "articles")
    pk = next(c for c in article.columns if c.primary_key)
    assert pk.name == "id"
    assert pk.nullable is False


def test_legacy_column_string_with_length() -> None:
    parser = SQLAlchemyParser()
    tables = parser.parse_file(FIXTURE)
    article = next(t for t in tables if t.name == "articles")
    title = next(c for c in article.columns if c.name == "title")
    assert title.type == "string"
    assert title.max_length == 500
    assert title.index is True
    assert title.nullable is False


def test_legacy_column_nullable_false() -> None:
    parser = SQLAlchemyParser()
    tables = parser.parse_file(FIXTURE)
    article = next(t for t in tables if t.name == "articles")
    is_draft = next(c for c in article.columns if c.name == "is_draft")
    assert is_draft.nullable is False
    assert is_draft.type == "bool"


def test_legacy_column_nullable_true() -> None:
    parser = SQLAlchemyParser()
    tables = parser.parse_file(FIXTURE)
    article = next(t for t in tables if t.name == "articles")
    body = next(c for c in article.columns if c.name == "body")
    assert body.nullable is True


def test_legacy_column_foreignkey() -> None:
    parser = SQLAlchemyParser()
    result = parser._parse_file_internal(FIXTURE)
    article_rels = [r for r in result.relations if r.from_table == "articles"]
    assert len(article_rels) >= 1
    fk_rel = next(r for r in article_rels if r.from_column == "author_id")
    assert fk_rel.to_table == "members"
    assert fk_rel.to_column == "id"


def test_schema_qualified_foreign_key_relation() -> None:
    """Schema-qualified FK like 'schema.table.column' should resolve correctly."""
    source = """
        from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
        from sqlalchemy import ForeignKey

        class Base(DeclarativeBase):
            pass

        class Session(Base):
            __tablename__ = "sessions"
            id: Mapped[int] = mapped_column(primary_key=True)
            user_id: Mapped[int] = mapped_column(ForeignKey("alpha_ai.users.id"))
    """
    result = parse_source_full(source)
    rels = [r for r in result.relations if r.from_table == "sessions"]
    assert len(rels) == 1
    assert rels[0].to_table == "users"
    assert rels[0].to_column == "id"


def test_schema_qualified_foreign_key_column_preserved() -> None:
    """Column.foreign_key must store the verbatim string including schema prefix.

    Bug fix (v0.1.3): previously the schema prefix was stripped, which broke
    SQLAlchemy's cross-schema FK resolution.  Now the full string is preserved.
    """
    source = """
        from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
        from sqlalchemy import ForeignKey

        class Base(DeclarativeBase):
            pass

        class Session(Base):
            __tablename__ = "sessions"
            id: Mapped[int] = mapped_column(primary_key=True)
            user_id: Mapped[int] = mapped_column(ForeignKey("alpha_ai.users.id"))
    """
    tables = parse_source(source)
    session = next(t for t in tables if t.name == "sessions")
    col = next(c for c in session.columns if c.name == "user_id")
    assert col.foreign_key == "alpha_ai.users.id"


def test_legacy_column_unique() -> None:
    parser = SQLAlchemyParser()
    tables = parser.parse_file(FIXTURE)
    tag = next(t for t in tables if t.name == "tags")
    name_col = next(c for c in tag.columns if c.name == "name")
    assert name_col.unique is True


def test_legacy_column_string_no_length() -> None:
    parser = SQLAlchemyParser()
    tables = parser.parse_file(FIXTURE)
    article = next(t for t in tables if t.name == "articles")
    body = next(c for c in article.columns if c.name == "body")
    assert body.type == "string"
    assert body.max_length is None


# ---------------------------------------------------------------------------
# 5. Mixed styles + custom inline source
# ---------------------------------------------------------------------------


def test_mixed_style_single_file() -> None:
    source = """
        import uuid
        from sqlalchemy import Column, Integer, String, ForeignKey
        from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

        class Base(DeclarativeBase):
            pass

        class Project(Base):
            __tablename__ = "projects"
            id: Mapped[int] = mapped_column(primary_key=True)
            name: Mapped[str] = mapped_column()

        class Issue(Base):
            __tablename__ = "issues"
            id = Column(Integer, primary_key=True)
            title = Column(String(200), nullable=False)
            project_id = Column(Integer, ForeignKey("projects.id"))
    """
    tables = parse_source(source)
    names = {t.name for t in tables}
    assert "projects" in names
    assert "issues" in names


def test_tablename_used_from_attr() -> None:
    source = """
        from sqlalchemy import Column, Integer
        from sqlalchemy.orm import DeclarativeBase

        class Base(DeclarativeBase):
            pass

        class MyWidget(Base):
            __tablename__ = "widgets"
            id = Column(Integer, primary_key=True)
    """
    tables = parse_source(source)
    assert tables[0].name == "widgets"


# ---------------------------------------------------------------------------
# 6. ORM detection — detect_project_orm()
# ---------------------------------------------------------------------------


def test_detect_project_orm_sqlmodel(tmp_path: Path) -> None:
    f = tmp_path / "models.py"
    f.write_text("from sqlmodel import SQLModel\n")
    assert detect_project_orm(tmp_path) == "sqlmodel"


def test_detect_project_orm_sqlalchemy(tmp_path: Path) -> None:
    f = tmp_path / "models.py"
    f.write_text("from sqlalchemy.orm import DeclarativeBase\n")
    assert detect_project_orm(tmp_path) == "sqlalchemy"


def test_detect_project_orm_empty_defaults_sqlmodel(tmp_path: Path) -> None:
    # No Python files → defaults to sqlmodel
    assert detect_project_orm(tmp_path) == "sqlmodel"


def test_detect_project_orm_both_raises(tmp_path: Path) -> None:
    """Genuine conflict: both parsers find actual ORM table class definitions."""
    from alter.errors import ParseError
    # SQLModel table definition (table=True marker)
    (tmp_path / "a.py").write_text(
        "from sqlmodel import SQLModel, Field\n"
        "class User(SQLModel, table=True):\n"
        "    __tablename__ = 'user'\n"
        "    id: int\n"
    )
    # Pure SQLAlchemy table definition (__tablename__ without table=True)
    (tmp_path / "b.py").write_text(
        "from sqlalchemy import Column, Integer\n"
        "from sqlalchemy.orm import DeclarativeBase\n"
        "class Base(DeclarativeBase): pass\n"
        "class Widget(Base):\n"
        "    __tablename__ = 'widget'\n"
        "    id = Column(Integer, primary_key=True)\n"
    )
    with pytest.raises(ParseError, match="(?i)both"):
        detect_project_orm(tmp_path)


# ---------------------------------------------------------------------------
# 6b. detect_project_orm() — SQLModel + SQLAlchemy co-existence (regression)
# ---------------------------------------------------------------------------
#
# SQLModel is built on SQLAlchemy.  Many SQLModel projects legitimately import
# from both (event listeners, custom Column types, advanced relationship
# config).  detect_project_orm() must resolve these as "sqlmodel" rather than
# raising a ParseError.
# ---------------------------------------------------------------------------


_SQLMODEL_TABLE = (
    "from sqlmodel import SQLModel, Field\n"
    "class User(SQLModel, table=True):\n"
    "    __tablename__ = 'user'\n"
    "    id: int = Field(primary_key=True)\n"
)

_SQLALCHEMY_UTILITY = (
    "from sqlalchemy import event\n"
    "def after_commit(session, *args): pass\n"
)

_SQLALCHEMY_COLUMN_IMPORT = (
    "from sqlalchemy import Column, String\n"
    "# Used alongside SQLModel for raw column types\n"
)

_SQLALCHEMY_ORM_IMPORT = (
    "from sqlalchemy.orm import relationship\n"
    "# Advanced relationship configuration\n"
)


def test_detect_orm_sqlmodel_with_sqlalchemy_event_import(tmp_path: Path) -> None:
    """SQLModel project that also imports sqlalchemy.event → 'sqlmodel'."""
    (tmp_path / "models.py").write_text(_SQLMODEL_TABLE)
    (tmp_path / "events.py").write_text(_SQLALCHEMY_UTILITY)
    assert detect_project_orm(tmp_path) == "sqlmodel"


def test_detect_orm_sqlmodel_with_sqlalchemy_column_import(tmp_path: Path) -> None:
    """SQLModel project with direct 'from sqlalchemy import Column' → 'sqlmodel'."""
    (tmp_path / "models.py").write_text(_SQLMODEL_TABLE)
    (tmp_path / "types.py").write_text(_SQLALCHEMY_COLUMN_IMPORT)
    assert detect_project_orm(tmp_path) == "sqlmodel"


def test_detect_orm_sqlmodel_with_sqlalchemy_orm_import(tmp_path: Path) -> None:
    """SQLModel project with 'from sqlalchemy.orm import relationship' → 'sqlmodel'."""
    (tmp_path / "models.py").write_text(_SQLMODEL_TABLE)
    (tmp_path / "relations.py").write_text(_SQLALCHEMY_ORM_IMPORT)
    assert detect_project_orm(tmp_path) == "sqlmodel"


def test_detect_orm_sqlmodel_with_multiple_sqlalchemy_files(tmp_path: Path) -> None:
    """Multiple sqlalchemy-importing utility files do not confuse detection."""
    (tmp_path / "models.py").write_text(_SQLMODEL_TABLE)
    (tmp_path / "events.py").write_text(_SQLALCHEMY_UTILITY)
    (tmp_path / "types.py").write_text(_SQLALCHEMY_COLUMN_IMPORT)
    (tmp_path / "relations.py").write_text(_SQLALCHEMY_ORM_IMPORT)
    assert detect_project_orm(tmp_path) == "sqlmodel"


def test_detect_orm_import_only_both_defaults_sqlmodel(tmp_path: Path) -> None:
    """Only imports (no table definitions) from both → defaults to 'sqlmodel'."""
    (tmp_path / "a.py").write_text("from sqlmodel import SQLModel\n")
    (tmp_path / "b.py").write_text("from sqlalchemy import Column\n")
    # Neither file defines actual table classes → no conflict, default sqlmodel
    assert detect_project_orm(tmp_path) == "sqlmodel"


def test_detect_orm_sqlalchemy_only_tables_no_sqlmodel_table(tmp_path: Path) -> None:
    """SQLAlchemy-only table definitions with a SQLModel utility import → 'sqlalchemy'."""
    # This file imports sqlmodel (e.g. for a mixin type) but defines no tables
    (tmp_path / "mixin.py").write_text("from sqlmodel import SQLModel\n# type helper\n")
    # This file defines a pure SQLAlchemy table
    (tmp_path / "models.py").write_text(
        "from sqlalchemy import Column, Integer\n"
        "from sqlalchemy.orm import DeclarativeBase\n"
        "class Base(DeclarativeBase): pass\n"
        "class Item(Base):\n"
        "    __tablename__ = 'item'\n"
        "    id = Column(Integer, primary_key=True)\n"
    )
    assert detect_project_orm(tmp_path) == "sqlalchemy"


def test_detect_orm_sqlmodel_table_true_in_same_file_as_sqlalchemy_import(tmp_path: Path) -> None:
    """A single file importing both and defining a SQLModel table → 'sqlmodel'."""
    (tmp_path / "models.py").write_text(
        "from sqlmodel import SQLModel, Field\n"
        "from sqlalchemy import event\n"
        "class User(SQLModel, table=True):\n"
        "    __tablename__ = 'user'\n"
        "    id: int = Field(primary_key=True)\n"
    )
    assert detect_project_orm(tmp_path) == "sqlmodel"


# ---------------------------------------------------------------------------
# 7. parse_directory
# ---------------------------------------------------------------------------


def test_parse_directory_returns_schema(tmp_path: Path) -> None:
    f = tmp_path / "models.py"
    f.write_text(dedent("""
        from sqlalchemy import Column, Integer
        from sqlalchemy.orm import DeclarativeBase

        class Base(DeclarativeBase):
            pass

        class Widget(Base):
            __tablename__ = "widgets"
            id = Column(Integer, primary_key=True)
    """))
    parser = SQLAlchemyParser(project_root=tmp_path)
    result = parser.parse_directory(tmp_path)
    assert isinstance(result.schema, AlterSchema)
    assert result.schema.orm == "sqlalchemy"
    assert any(t.name == "widgets" for t in result.schema.tables)


def test_parse_directory_skips_syntax_errors(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("from sqlalchemy import\nclass (((\n")
    good = tmp_path / "good.py"
    good.write_text(dedent("""
        from sqlalchemy import Column, Integer
        from sqlalchemy.orm import DeclarativeBase

        class Base(DeclarativeBase):
            pass

        class Widget(Base):
            __tablename__ = "widgets"
            id = Column(Integer, primary_key=True)
    """))
    parser = SQLAlchemyParser(project_root=tmp_path)
    result = parser.parse_directory(tmp_path)
    assert len(result.skipped_files) == 1
    assert len(result.schema.tables) == 1


# ---------------------------------------------------------------------------
# Round-trip fidelity regression tests
# ---------------------------------------------------------------------------


def test_bug4_dict_literal_default_preserved() -> None:
    """default={} should not be dropped in SQLAlchemy parser."""
    source = """
        from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
        from sqlalchemy import JSON

        class Base(DeclarativeBase):
            pass

        class Config(Base):
            __tablename__ = "configs"
            id: Mapped[int] = mapped_column(primary_key=True)
            data: Mapped[dict] = mapped_column(default={})
    """
    tables = parse_source(source)
    config = next(t for t in tables if t.name == "configs")
    col = next(c for c in config.columns if c.name == "data")
    assert col.default == "{}"


def test_bug5_datetime_now_preserved() -> None:
    """Generator emits datetime.now for 'now' and the modern timezone-aware
    equivalent for 'utcnow' (datetime.utcnow is deprecated since Python 3.12)."""
    from alter.generators.sqlalchemy import _mapped_column_args
    from alter.schema import Column
    now_col = Column(name="created_at", type="datetime", default="now")
    utcnow_col = Column(name="synced_at", type="datetime", default="utcnow")
    assert "datetime.now" in _mapped_column_args(now_col, set())
    assert "datetime.now(timezone.utc)" in _mapped_column_args(utcnow_col, set())
    assert "datetime.utcnow" not in _mapped_column_args(utcnow_col, set())


def test_bug6_enum_member_names_preserved() -> None:
    """Enum member names should be stored, not just values."""
    from alter.schema import EnumMember
    source = """
        import enum

        class Priority(str, enum.Enum):
            HIGH = "high"
            LOW = "low"
    """
    result = parse_source_full(source)
    assert len(result.enums) == 1
    members = result.enums[0].values
    assert len(members) == 2
    assert isinstance(members[0], EnumMember)
    assert members[0].member_name == "HIGH"
    assert members[0].value == "high"
