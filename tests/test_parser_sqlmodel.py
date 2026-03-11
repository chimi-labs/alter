"""Tests for the SQLModel parser backend.

All tests use static source text or the SaaS starter fixture —
no user code is ever imported or executed.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from alter.parsers.sqlmodel import SQLModelParser
from alter.schema import AlterSchema, Column, EnumDef, Table

# Paths to the SaaS starter example (models are now split across files)
SAAS_ROOT = Path(__file__).parent.parent / "examples" / "saas-starter"
SAAS_APP = SAAS_ROOT / "app"
SAAS_MODELS = SAAS_APP / "models" / "starter.py"
SAAS_ENUMS = SAAS_APP / "enums.py"
SAAS_PARENTS = SAAS_APP / "models" / "parents.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse_source(source: str, project_root: Path | None = None) -> list[Table]:
    """Write source to a tmp file and parse it."""
    import tempfile, os
    with tempfile.NamedTemporaryFile(
        suffix=".py", mode="w", delete=False, encoding="utf-8"
    ) as f:
        f.write(dedent(source))
        tmp = Path(f.name)
    try:
        parser = SQLModelParser(project_root=project_root)
        return parser.parse_file(tmp)
    finally:
        os.unlink(tmp)


def parse_source_full(source: str):
    """Return the _FileResult for richer assertions."""
    import tempfile, os
    from alter.parsers.sqlmodel import SQLModelParser
    with tempfile.NamedTemporaryFile(
        suffix=".py", mode="w", delete=False, encoding="utf-8"
    ) as f:
        f.write(dedent(source))
        tmp = Path(f.name)
    try:
        parser = SQLModelParser()
        return parser._parse_file_internal(tmp)
    finally:
        os.unlink(tmp)


# ---------------------------------------------------------------------------
# 1. ORM detection
# ---------------------------------------------------------------------------


def test_detect_orm_sqlmodel_file(tmp_path: Path) -> None:
    f = tmp_path / "models.py"
    f.write_text("from sqlmodel import SQLModel, Field\n")
    assert SQLModelParser().detect_orm(f) is True


def test_detect_orm_non_sqlmodel_file(tmp_path: Path) -> None:
    f = tmp_path / "models.py"
    f.write_text("import os\n")
    assert SQLModelParser().detect_orm(f) is False


def test_detect_orm_missing_file(tmp_path: Path) -> None:
    f = tmp_path / "nope.py"
    assert SQLModelParser().detect_orm(f) is False


# ---------------------------------------------------------------------------
# 2. Table detection — only table=True classes
# ---------------------------------------------------------------------------


def test_skips_plain_sqlmodel_class() -> None:
    source = """
        from sqlmodel import SQLModel, Field

        class Schema(SQLModel):
            name: str = Field(...)

        class User(SQLModel, table=True):
            id: int = Field(primary_key=True)
            name: str = Field()
    """
    tables = parse_source(source)
    assert len(tables) == 1
    assert tables[0].name == "user"


def test_detects_multiple_table_classes() -> None:
    source = """
        from sqlmodel import SQLModel, Field

        class User(SQLModel, table=True):
            id: int = Field(primary_key=True)
            name: str = Field()

        class Post(SQLModel, table=True):
            id: int = Field(primary_key=True)
            title: str = Field()
    """
    tables = parse_source(source)
    names = {t.name for t in tables}
    assert "user" in names
    assert "post" in names


# ---------------------------------------------------------------------------
# 3. __tablename__
# ---------------------------------------------------------------------------


def test_tablename_from_attribute() -> None:
    source = """
        from sqlmodel import SQLModel, Field

        class User(SQLModel, table=True):
            __tablename__ = "users"
            id: int = Field(primary_key=True)
    """
    tables = parse_source(source)
    assert tables[0].name == "users"


def test_tablename_defaults_to_class_name_lower() -> None:
    source = """
        from sqlmodel import SQLModel, Field

        class BlogPost(SQLModel, table=True):
            id: int = Field(primary_key=True)
    """
    tables = parse_source(source)
    assert tables[0].name == "blogpost"


# ---------------------------------------------------------------------------
# 4. Primary key
# ---------------------------------------------------------------------------


def test_primary_key_field() -> None:
    source = """
        import uuid
        from sqlmodel import SQLModel, Field

        class User(SQLModel, table=True):
            __tablename__ = "users"
            id: uuid.UUID = Field(primary_key=True)
    """
    tables = parse_source(source)
    col = tables[0].columns[0]
    assert col.primary_key is True
    assert col.nullable is False
    assert col.type == "uuid"


# ---------------------------------------------------------------------------
# 5. Type hints
# ---------------------------------------------------------------------------


def test_str_type_hint() -> None:
    source = """
        from sqlmodel import SQLModel, Field

        class Item(SQLModel, table=True):
            id: int = Field(primary_key=True)
            name: str = Field()
    """
    tables = parse_source(source)
    name_col = next(c for c in tables[0].columns if c.name == "name")
    assert name_col.type == "string"


def test_bool_type_hint() -> None:
    source = """
        from sqlmodel import SQLModel, Field

        class Item(SQLModel, table=True):
            id: int = Field(primary_key=True)
            active: bool = Field(default=True)
    """
    tables = parse_source(source)
    col = next(c for c in tables[0].columns if c.name == "active")
    assert col.type == "bool"


def test_optional_makes_column_nullable() -> None:
    source = """
        from typing import Optional
        from sqlmodel import SQLModel, Field

        class Post(SQLModel, table=True):
            id: int = Field(primary_key=True)
            body: Optional[str] = Field(default=None)
    """
    tables = parse_source(source)
    body_col = next(c for c in tables[0].columns if c.name == "body")
    assert body_col.nullable is True
    assert body_col.type == "string"


def test_non_optional_is_not_nullable() -> None:
    source = """
        from sqlmodel import SQLModel, Field

        class Post(SQLModel, table=True):
            id: int = Field(primary_key=True)
            title: str = Field()
    """
    tables = parse_source(source)
    col = next(c for c in tables[0].columns if c.name == "title")
    assert col.nullable is False


def test_uuid_qualified_type_hint() -> None:
    source = """
        import uuid
        from sqlmodel import SQLModel, Field

        class User(SQLModel, table=True):
            id: uuid.UUID = Field(primary_key=True)
    """
    tables = parse_source(source)
    assert tables[0].columns[0].type == "uuid"


def test_datetime_type_hint() -> None:
    source = """
        from datetime import datetime
        from sqlmodel import SQLModel, Field

        class Log(SQLModel, table=True):
            id: int = Field(primary_key=True)
            created_at: datetime = Field()
    """
    tables = parse_source(source)
    col = next(c for c in tables[0].columns if c.name == "created_at")
    assert col.type == "datetime"


# ---------------------------------------------------------------------------
# 6. Field() arguments
# ---------------------------------------------------------------------------


def test_field_unique() -> None:
    source = """
        from sqlmodel import SQLModel, Field

        class User(SQLModel, table=True):
            id: int = Field(primary_key=True)
            email: str = Field(unique=True)
    """
    tables = parse_source(source)
    col = next(c for c in tables[0].columns if c.name == "email")
    assert col.unique is True


def test_field_index() -> None:
    source = """
        from sqlmodel import SQLModel, Field

        class User(SQLModel, table=True):
            id: int = Field(primary_key=True)
            email: str = Field(index=True)
    """
    tables = parse_source(source)
    col = next(c for c in tables[0].columns if c.name == "email")
    assert col.index is True


def test_field_max_length() -> None:
    source = """
        from sqlmodel import SQLModel, Field

        class User(SQLModel, table=True):
            id: int = Field(primary_key=True)
            name: str = Field(max_length=100)
    """
    tables = parse_source(source)
    col = next(c for c in tables[0].columns if c.name == "name")
    assert col.max_length == 100


def test_field_default_string_literal() -> None:
    source = """
        from sqlmodel import SQLModel, Field

        class Plan(SQLModel, table=True):
            id: int = Field(primary_key=True)
            tier: str = Field(default="free")
    """
    tables = parse_source(source)
    col = next(c for c in tables[0].columns if c.name == "tier")
    assert col.default == "free"


def test_field_default_factory_uuid() -> None:
    source = """
        import uuid
        from sqlmodel import SQLModel, Field

        class User(SQLModel, table=True):
            id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    """
    tables = parse_source(source)
    col = tables[0].columns[0]
    assert col.default == "uuid4"


def test_field_default_none_sets_nullable() -> None:
    source = """
        from typing import Optional
        from sqlmodel import SQLModel, Field

        class User(SQLModel, table=True):
            id: int = Field(primary_key=True)
            bio: Optional[str] = Field(default=None)
    """
    tables = parse_source(source)
    col = next(c for c in tables[0].columns if c.name == "bio")
    assert col.nullable is True


def test_field_bool_default() -> None:
    source = """
        from sqlmodel import SQLModel, Field

        class Post(SQLModel, table=True):
            id: int = Field(primary_key=True)
            is_published: bool = Field(default=False)
    """
    tables = parse_source(source)
    col = next(c for c in tables[0].columns if c.name == "is_published")
    assert col.default == "false"


# ---------------------------------------------------------------------------
# 7. Foreign keys → Relations
# ---------------------------------------------------------------------------


def test_foreign_key_creates_relation() -> None:
    source = """
        from sqlmodel import SQLModel, Field

        class Post(SQLModel, table=True):
            __tablename__ = "posts"
            id: int = Field(primary_key=True)
            author_id: int = Field(foreign_key="users.id")
    """
    result = parse_source_full(source)
    assert len(result.relations) == 1
    rel = result.relations[0]
    assert rel.from_table == "posts"
    assert rel.from_column == "author_id"
    assert rel.to_table == "users"
    assert rel.to_column == "id"


def test_foreign_key_column_has_fk_field() -> None:
    source = """
        from sqlmodel import SQLModel, Field

        class Post(SQLModel, table=True):
            id: int = Field(primary_key=True)
            author_id: int = Field(foreign_key="users.id")
    """
    tables = parse_source(source)
    col = next(c for c in tables[0].columns if c.name == "author_id")
    assert col.foreign_key == "users.id"


def test_schema_qualified_foreign_key_relation() -> None:
    """Schema-qualified FK like 'schema.table.column' should resolve correctly."""
    source = """
        from sqlmodel import SQLModel, Field

        class Session(SQLModel, table=True):
            __tablename__ = "sessions"
            id: int = Field(primary_key=True)
            user_id: int = Field(foreign_key="alpha_ai.users.id")
    """
    result = parse_source_full(source)
    assert len(result.relations) == 1
    rel = result.relations[0]
    assert rel.from_table == "sessions"
    assert rel.from_column == "user_id"
    assert rel.to_table == "users"
    assert rel.to_column == "id"


def test_schema_qualified_foreign_key_column_preserved() -> None:
    """Column.foreign_key must store the verbatim string including schema prefix.

    Bug fix (v0.1.3): previously the schema prefix was stripped, which broke
    cross-schema FK round-trips.  Now the full string is preserved.
    """
    source = """
        from sqlmodel import SQLModel, Field

        class Session(SQLModel, table=True):
            id: int = Field(primary_key=True)
            user_id: int = Field(foreign_key="alpha_ai.users.id")
    """
    tables = parse_source(source)
    col = next(c for c in tables[0].columns if c.name == "user_id")
    assert col.foreign_key == "alpha_ai.users.id"


# ---------------------------------------------------------------------------
# 8. Relationship back-references — should be skipped as columns
# ---------------------------------------------------------------------------


def test_relationship_fields_skipped() -> None:
    source = """
        from typing import Optional
        from sqlmodel import SQLModel, Field, Relationship

        class User(SQLModel, table=True):
            __tablename__ = "users"
            id: int = Field(primary_key=True)
            name: str = Field()
            posts: list["Post"] = Relationship(back_populates="author")
    """
    tables = parse_source(source)
    col_names = [c.name for c in tables[0].columns]
    assert "posts" not in col_names
    assert "id" in col_names
    assert "name" in col_names


def test_optional_relationship_back_ref_skipped() -> None:
    source = """
        from typing import Optional
        from sqlmodel import SQLModel, Field, Relationship

        class Membership(SQLModel, table=True):
            __tablename__ = "memberships"
            id: int = Field(primary_key=True)
            user: Optional["User"] = Relationship(back_populates="memberships")
    """
    tables = parse_source(source)
    col_names = [c.name for c in tables[0].columns]
    assert "user" not in col_names


# ---------------------------------------------------------------------------
# 9. Enum classes
# ---------------------------------------------------------------------------


def test_enum_classes_detected() -> None:
    source = """
        from enum import Enum
        from sqlmodel import SQLModel, Field

        class Role(str, Enum):
            admin = "admin"
            member = "member"

        class User(SQLModel, table=True):
            id: int = Field(primary_key=True)
            role: Role = Field(default=Role.member)
    """
    result = parse_source_full(source)
    assert len(result.enums) == 1
    assert result.enums[0].name == "Role"
    values = [v.value for v in result.enums[0].values]
    assert "admin" in values
    assert "member" in values


def test_enum_column_type_is_enum_name() -> None:
    source = """
        from enum import Enum
        from sqlmodel import SQLModel, Field

        class Role(str, Enum):
            admin = "admin"
            member = "member"

        class Membership(SQLModel, table=True):
            id: int = Field(primary_key=True)
            role: Role = Field(default=Role.member)
    """
    tables = parse_source(source)
    col = next(c for c in tables[0].columns if c.name == "role")
    assert col.type == "Role"


def test_enum_default_value_extracted() -> None:
    source = """
        from enum import Enum
        from sqlmodel import SQLModel, Field

        class Status(str, Enum):
            active = "active"
            inactive = "inactive"

        class Account(SQLModel, table=True):
            id: int = Field(primary_key=True)
            status: Status = Field(default=Status.active)
    """
    tables = parse_source(source)
    col = next(c for c in tables[0].columns if c.name == "status")
    assert col.default == "active"


# ---------------------------------------------------------------------------
# 10. file_path population
# ---------------------------------------------------------------------------


def test_file_path_relative_to_project_root(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    models_file = app_dir / "models.py"
    models_file.write_text(dedent("""
        from sqlmodel import SQLModel, Field

        class User(SQLModel, table=True):
            id: int = Field(primary_key=True)
    """))
    parser = SQLModelParser(project_root=tmp_path)
    tables = parser.parse_file(models_file)
    assert tables[0].file_path == "app/models.py"


# ---------------------------------------------------------------------------
# 11. Error recovery
# ---------------------------------------------------------------------------


def test_syntax_error_raises_parse_error(tmp_path: Path) -> None:
    from alter.errors import ParseError
    bad = tmp_path / "bad.py"
    bad.write_text("from sqlmodel import\nclass (((\n")
    parser = SQLModelParser()
    with pytest.raises(ParseError):
        parser.parse_file(bad)


def test_parse_directory_skips_syntax_errors(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("from sqlmodel import\nclass (((\n")
    good = tmp_path / "good.py"
    good.write_text(dedent("""
        from sqlmodel import SQLModel, Field

        class Item(SQLModel, table=True):
            id: int = Field(primary_key=True)
    """))
    parser = SQLModelParser(project_root=tmp_path)
    result = parser.parse_directory(tmp_path)
    assert len(result.skipped_files) == 1
    assert len(result.schema.tables) == 1


# ---------------------------------------------------------------------------
# 12. SaaS starter integration (models split across app/enums.py,
#     app/models/parents.py, app/models/starter.py)
# ---------------------------------------------------------------------------


def test_saas_starter_parse_directory_tables() -> None:
    """parse_directory should find all 8 table classes."""
    parser = SQLModelParser(project_root=SAAS_ROOT)
    result = parser.parse_directory(SAAS_APP)
    names = {t.name for t in result.schema.tables}
    assert "users" in names
    assert "organizations" in names
    assert "memberships" in names
    assert "subscriptions" in names
    assert "invoices" in names
    assert "posts" in names
    assert "audit_logs" in names
    assert len(result.schema.tables) >= 7
    assert len(result.skipped_files) == 0


def test_saas_starter_user_columns_with_inheritance() -> None:
    """User inherits id/created_at/updated_at/update_source from base classes."""
    parser = SQLModelParser(project_root=SAAS_ROOT)
    result = parser.parse_directory(SAAS_APP)
    user = next(t for t in result.schema.tables if t.name == "users")
    col_names = {c.name for c in user.columns}
    # Locally defined
    assert "name" in col_names
    assert "email" in col_names
    assert "is_active" in col_names
    # Inherited from UUIDBase
    assert "id" in col_names
    # Inherited from TimestampedBase
    assert "created_at" in col_names
    assert "updated_at" in col_names
    assert "update_source" in col_names
    # Relationship back-refs must be absent
    assert "memberships" not in col_names
    assert "posts" not in col_names


def test_saas_starter_email_column_properties() -> None:
    parser = SQLModelParser(project_root=SAAS_ROOT)
    result = parser.parse_directory(SAAS_APP)
    user = next(t for t in result.schema.tables if t.name == "users")
    email = next(c for c in user.columns if c.name == "email")
    assert email.unique is True
    assert email.index is True
    assert email.max_length == 255
    assert email.type == "string"


def test_saas_starter_uuid_pk_from_base() -> None:
    """UUID primary key is inherited from UUIDBase, not defined in User body."""
    parser = SQLModelParser(project_root=SAAS_ROOT)
    result = parser.parse_directory(SAAS_APP)
    user = next(t for t in result.schema.tables if t.name == "users")
    pk = next(c for c in user.columns if c.primary_key)
    assert pk.name == "id"
    assert pk.type == "uuid"
    assert pk.nullable is False


def test_saas_starter_enums_detected_from_enum_file() -> None:
    """Enums defined in app/enums.py are collected via the two-phase pre-scan."""
    parser = SQLModelParser(project_root=SAAS_ROOT)
    result = parser.parse_directory(SAAS_APP)
    enum_names = {e.name for e in result.schema.enums}
    assert "Role" in enum_names
    assert "SubscriptionStatus" in enum_names
    assert "InvoiceStatus" in enum_names
    assert "UpdateSource" in enum_names


def test_saas_starter_foreign_keys_produce_relations() -> None:
    parser = SQLModelParser(project_root=SAAS_ROOT)
    result = parser.parse_directory(SAAS_APP)
    from_tables = {r.from_table for r in result.schema.relations}
    assert "memberships" in from_tables
    assert "posts" in from_tables


def test_saas_starter_optional_columns_nullable() -> None:
    parser = SQLModelParser(project_root=SAAS_ROOT)
    result = parser.parse_directory(SAAS_APP)
    post = next(t for t in result.schema.tables if t.name == "posts")
    body = next(c for c in post.columns if c.name == "body")
    assert body.nullable is True


def test_saas_starter_file_path_relative() -> None:
    """Table file_path should be relative to project root."""
    parser = SQLModelParser(project_root=SAAS_ROOT)
    result = parser.parse_directory(SAAS_APP)
    user = next(t for t in result.schema.tables if t.name == "users")
    assert user.file_path == "app/models/starter.py"


def test_saas_starter_parse_directory_produces_schema() -> None:
    parser = SQLModelParser(project_root=SAAS_ROOT)
    result = parser.parse_directory(SAAS_APP)
    assert isinstance(result.schema, AlterSchema)
    assert result.schema.orm == "sqlmodel"
    assert len(result.schema.tables) >= 7
    assert len(result.skipped_files) == 0


# ---------------------------------------------------------------------------
# 13. Cross-file enum and inheritance resolution
# ---------------------------------------------------------------------------


def test_cross_file_enum_resolves_via_parse_file_result(tmp_path: Path) -> None:
    """parse_file_result follows imports to resolve enums from sibling files."""
    # Create a mini package: enums.py + models.py
    (tmp_path / "__init__.py").write_text("")
    enums_py = tmp_path / "enums.py"
    enums_py.write_text(dedent("""
        from enum import Enum

        class Status(str, Enum):
            active = "active"
            inactive = "inactive"
    """))
    models_py = tmp_path / "models.py"
    models_py.write_text(dedent(f"""
        from sqlmodel import SQLModel, Field
        from enums import Status

        class Item(SQLModel, table=True):
            id: int = Field(primary_key=True)
            status: Status = Field(default=Status.active)
    """))

    parser = SQLModelParser(project_root=tmp_path)
    result = parser.parse_file_result(models_py)

    # The enum should be included
    enum_names = {e.name for e in result.schema.enums}
    assert "Status" in enum_names

    # The column type should resolve to "Status", not "string"
    item = next(t for t in result.schema.tables if t.name == "item")
    status_col = next(c for c in item.columns if c.name == "status")
    assert status_col.type == "Status"


def test_cross_file_base_class_columns_inherited(tmp_path: Path) -> None:
    """Columns defined in a non-table SQLModel base class are inherited."""
    (tmp_path / "__init__.py").write_text("")
    base_py = tmp_path / "base.py"
    base_py.write_text(dedent("""
        import uuid
        from sqlmodel import SQLModel, Field

        class UUIDBase(SQLModel):
            id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    """))
    models_py = tmp_path / "models.py"
    models_py.write_text(dedent("""
        from sqlmodel import SQLModel, Field
        from base import UUIDBase

        class User(UUIDBase, table=True):
            __tablename__ = "users"
            name: str = Field(max_length=100)
    """))

    parser = SQLModelParser(project_root=tmp_path)
    result = parser.parse_file_result(models_py)

    user = next(t for t in result.schema.tables if t.name == "users")
    col_names = {c.name for c in user.columns}
    # Inherited column
    assert "id" in col_names
    pk = next(c for c in user.columns if c.name == "id")
    assert pk.primary_key is True
    assert pk.type == "uuid"
    # Local column
    assert "name" in col_names


def test_cross_file_enum_in_two_phase_directory_scan(tmp_path: Path) -> None:
    """parse_directory pre-scan collects enums from files with no ORM import."""
    # enums.py has no SQLModel import — previously would be skipped
    enums_py = tmp_path / "enums.py"
    enums_py.write_text(dedent("""
        from enum import Enum

        class Color(str, Enum):
            red = "red"
            blue = "blue"
    """))
    models_py = tmp_path / "models.py"
    models_py.write_text(dedent("""
        from sqlmodel import SQLModel, Field
        from enums import Color

        class Widget(SQLModel, table=True):
            id: int = Field(primary_key=True)
            color: Color = Field(default=Color.red)
    """))

    parser = SQLModelParser(project_root=tmp_path)
    result = parser.parse_directory(tmp_path)

    # Enum from enum-only file should be present
    enum_names = {e.name for e in result.schema.enums}
    assert "Color" in enum_names

    # Column type should be "Color", not "string"
    widget = next(t for t in result.schema.tables if t.name == "widget")
    color_col = next(c for c in widget.columns if c.name == "color")
    assert color_col.type == "Color"


def test_cross_file_inherited_enum_type(tmp_path: Path) -> None:
    """An enum type used in a base class column resolves correctly."""
    (tmp_path / "__init__.py").write_text("")
    enums_py = tmp_path / "enums.py"
    enums_py.write_text(dedent("""
        from enum import Enum

        class Source(str, Enum):
            manual = "manual"
            api = "api"
    """))
    base_py = tmp_path / "base.py"
    base_py.write_text(dedent("""
        from sqlmodel import SQLModel, Field
        from enums import Source

        class AuditBase(SQLModel):
            source: Source = Field(default=Source.manual)
    """))
    models_py = tmp_path / "models.py"
    models_py.write_text(dedent("""
        from sqlmodel import SQLModel, Field
        from base import AuditBase

        class Event(AuditBase, table=True):
            id: int = Field(primary_key=True)
    """))

    parser = SQLModelParser(project_root=tmp_path)
    result = parser.parse_directory(tmp_path)

    event = next(t for t in result.schema.tables if t.name == "event")
    col_names = {c.name for c in event.columns}
    assert "source" in col_names
    source_col = next(c for c in event.columns if c.name == "source")
    assert source_col.type == "Source"


def test_cross_file_enum_file_path_tracked(tmp_path: Path) -> None:
    """EnumDef.file_path records the file the enum was defined in."""
    enums_py = tmp_path / "enums.py"
    enums_py.write_text(dedent("""
        from enum import Enum

        class MyEnum(str, Enum):
            a = "a"
    """))
    models_py = tmp_path / "models.py"
    models_py.write_text(dedent("""
        from sqlmodel import SQLModel, Field
        from enums import MyEnum

        class Thing(SQLModel, table=True):
            id: int = Field(primary_key=True)
            kind: MyEnum = Field(default=MyEnum.a)
    """))

    parser = SQLModelParser(project_root=tmp_path)
    result = parser.parse_directory(tmp_path)

    my_enum = next(e for e in result.schema.enums if e.name == "MyEnum")
    assert my_enum.file_path == "enums.py"


# ---------------------------------------------------------------------------
# Round-trip fidelity regression tests (bugs 1-7)
# ---------------------------------------------------------------------------


def test_bug1_lambda_default_factory_preserved() -> None:
    """Lambda default_factory should not be silently dropped."""
    source = """
        import uuid
        from sqlmodel import SQLModel, Field

        class Token(SQLModel, table=True):
            id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    """
    tables = parse_source(source)
    col = next(c for c in tables[0].columns if c.name == "id")
    assert col.default is not None
    assert "lambda" in col.default
    assert "uuid" in col.default


def test_bug2_sa_column_preserved() -> None:
    """sa_column kwarg should be preserved via extra_kwargs."""
    source = """
        from sqlmodel import SQLModel, Field
        from sqlalchemy import Column, JSON

        class Config(SQLModel, table=True):
            id: int = Field(primary_key=True)
            data: dict = Field(sa_column=Column(JSON))
    """
    tables = parse_source(source)
    col = next(c for c in tables[0].columns if c.name == "data")
    assert col.extra_kwargs is not None
    assert "sa_column" in col.extra_kwargs


def test_bug3_bare_list_resolves_to_json_array() -> None:
    """Optional[list] should resolve to json_array (not json or string).

    Bug fix (v0.1.3): bare list now maps to the dedicated 'json_array' alter
    type so it round-trips back as list instead of dict.
    """
    from alter.types import python_to_alter
    assert python_to_alter("list") == "json_array"
    assert python_to_alter("List") == "json_array"


def test_bug4_dict_literal_default_preserved() -> None:
    """default={} should not be dropped."""
    source = """
        from sqlmodel import SQLModel, Field

        class Setting(SQLModel, table=True):
            id: int = Field(primary_key=True)
            data: dict = Field(default={})
    """
    tables = parse_source(source)
    col = next(c for c in tables[0].columns if c.name == "data")
    assert col.default == "{}"


def test_bug4_list_literal_default_preserved() -> None:
    """default=[] should not be dropped."""
    source = """
        from sqlmodel import SQLModel, Field

        class Setting(SQLModel, table=True):
            id: int = Field(primary_key=True)
            tags: list = Field(default=[])
    """
    tables = parse_source(source)
    col = next(c for c in tables[0].columns if c.name == "tags")
    assert col.default == "[]"


def test_bug5_datetime_now_not_converted_to_utcnow() -> None:
    """datetime.now should stay distinct from datetime.utcnow."""
    source = """
        from datetime import datetime
        from sqlmodel import SQLModel, Field

        class Event(SQLModel, table=True):
            id: int = Field(primary_key=True)
            created_at: datetime = Field(default_factory=datetime.now)
            synced_at: datetime = Field(default_factory=datetime.utcnow)
    """
    tables = parse_source(source)
    created = next(c for c in tables[0].columns if c.name == "created_at")
    synced = next(c for c in tables[0].columns if c.name == "synced_at")
    assert created.default == "now"
    assert synced.default == "utcnow"


def test_bug5_generator_emits_correct_datetime() -> None:
    """Generator should emit datetime.now for 'now' and datetime.utcnow for 'utcnow'."""
    from alter.generators.sqlmodel import _field_args
    now_col = Column(name="created_at", type="datetime", default="now")
    utcnow_col = Column(name="synced_at", type="datetime", default="utcnow")
    assert "datetime.now" in _field_args(now_col, set())
    assert "datetime.utcnow" not in _field_args(now_col, set())
    assert "datetime.utcnow" in _field_args(utcnow_col, set())


def test_bug6_enum_member_names_preserved() -> None:
    """Enum member names (ENDUSER) should be distinct from values (enduser)."""
    from alter.schema import EnumMember
    source = """
        from enum import Enum

        class ActorType(str, Enum):
            ENDUSER = "enduser"
            BOT = "bot"
            SYSTEM = "system"
    """
    result = parse_source_full(source)
    assert len(result.enums) == 1
    enum = result.enums[0]
    members = enum.values
    assert len(members) == 3
    m0 = members[0]
    assert isinstance(m0, EnumMember)
    assert m0.member_name == "ENDUSER"
    assert m0.value == "enduser"


def test_bug6_enum_class_source_uses_member_names() -> None:
    """Generator should use member_name for identifier, value for string."""
    from alter.schema import EnumDef, EnumMember
    from alter.generators.sqlmodel import _enum_class_source
    enum = EnumDef(name="ActorType", values=[
        EnumMember(member_name="ENDUSER", value="enduser"),
        EnumMember(member_name="BOT", value="bot"),
    ])
    src = _enum_class_source(enum)
    assert 'ENDUSER = "enduser"' in src
    assert 'BOT = "bot"' in src
    # Should NOT have lowercase member names
    assert "enduser = " not in src


def test_bug7_regex_preserved() -> None:
    """regex kwarg should be preserved via extra_kwargs."""
    source = """
        from sqlmodel import SQLModel, Field

        class Slug(SQLModel, table=True):
            id: int = Field(primary_key=True)
            slug: str = Field(regex=r"^[a-z_]+$")
    """
    tables = parse_source(source)
    col = next(c for c in tables[0].columns if c.name == "slug")
    assert col.extra_kwargs is not None
    assert "regex" in col.extra_kwargs


def test_bug7_ge_le_validators_preserved() -> None:
    """ge, le, gt, lt validators should be preserved via extra_kwargs."""
    source = """
        from sqlmodel import SQLModel, Field

        class Score(SQLModel, table=True):
            id: int = Field(primary_key=True)
            value: int = Field(ge=0, le=100)
    """
    tables = parse_source(source)
    col = next(c for c in tables[0].columns if c.name == "value")
    assert col.extra_kwargs is not None
    assert "ge" in col.extra_kwargs
    assert "le" in col.extra_kwargs
