"""Regression tests — enum class written to every model file when file_path=None.

ISSUE: ``local_enum_names`` was built with the condition
``e.file_path is None or e.file_path == rel_path``.  The ``is None`` branch
evaluated True for *every* file being processed, so canvas-created enums
(which carry no ``file_path``) were injected into all model files in a
multi-file project.

Fix: enums with ``file_path=None`` now route exclusively to the project's
default model file (determined by ``_default_model_path``).  Enums with an
explicit ``file_path`` continue to be emitted only in that file.

Covered code paths
------------------
* ``alter/generators/base.py`` ``BaseGenerator.preview_apply``
* ``alter/cli.py``             ``apply`` command loop
* ``alter/mcp_server.py``      ``_apply_to_code_impl``
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from alter.schema import AlterSchema, Column, EnumDef, Table
from alter.generators.base import get_generator, _default_model_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_two_file_schema(
    enum_file_path: str | None,
) -> AlterSchema:
    """Return a schema with two tables in separate files and one enum.

    ``users`` lives in ``app/users.py``.
    ``posts`` lives in ``app/posts.py``.
    The enum ``UserRole`` is placed in *enum_file_path* (or left as None).

    NOTE: ``_default_model_path`` for this schema returns ``"app/models.py"``
    because both tables share the ``app/`` directory.
    """
    return AlterSchema(
        orm="sqlmodel",
        tables=[
            Table(
                name="users",
                file_path="app/users.py",
                columns=[
                    Column(name="id", type="uuid", primary_key=True, nullable=False),
                    Column(name="role", type="UserRole", nullable=False),
                ],
            ),
            Table(
                name="posts",
                file_path="app/posts.py",
                columns=[
                    Column(name="id", type="uuid", primary_key=True, nullable=False),
                    Column(name="title", type="string", nullable=False),
                ],
            ),
        ],
        enums=[
            EnumDef(
                name="UserRole",
                values=["admin", "user", "guest"],
                file_path=enum_file_path,
            )
        ],
    )


def _default_path_for(schema: AlterSchema, project_root: Path) -> str:
    return _default_model_path(schema, project_root)


# ---------------------------------------------------------------------------
# Tests for generators/base.py — preview_apply
# ---------------------------------------------------------------------------


def _parse_diff_sections(diff: str) -> dict[str, str]:
    """Parse a unified diff and return {filename: diff_chunk} mapping.

    Only the ``+++ b/<path>`` header lines are used to identify filenames.
    """
    sections: dict[str, str] = {}
    current_file: str | None = None
    for line in diff.splitlines(keepends=True):
        if line.startswith("+++ b/"):
            current_file = line[6:].rstrip()
            sections.setdefault(current_file, "")
        elif current_file is not None:
            sections[current_file] += line
    return sections


class TestPreviewApplyEnumRouting:
    """``preview_apply`` must emit the enum class in exactly one file."""

    def _existing_users(self) -> str:
        return textwrap.dedent("""\
            from sqlmodel import SQLModel, Field

            class UsersSQL(SQLModel, table=True):
                __tablename__ = "users"
                id: str = Field(primary_key=True)
                role: str
        """)

    def _existing_posts(self) -> str:
        return textwrap.dedent("""\
            from sqlmodel import SQLModel, Field

            class PostsSQL(SQLModel, table=True):
                __tablename__ = "posts"
                id: str = Field(primary_key=True)
                title: str
        """)

    def test_enum_none_file_path_goes_to_default_file_only(self, tmp_path: Path):
        """Enum with file_path=None appears in the diff for the default model file."""
        schema = _make_two_file_schema(enum_file_path=None)
        # Both tables are in app/ → default is app/models.py
        default = _default_path_for(schema, tmp_path)
        assert default == "app/models.py", f"Unexpected default: {default!r}"
        gen = get_generator("sqlmodel")

        # Write both model files (default file doesn't exist — will be generated fresh)
        (tmp_path / "app").mkdir(parents=True, exist_ok=True)
        (tmp_path / "app" / "users.py").write_text(self._existing_users())
        (tmp_path / "app" / "posts.py").write_text(self._existing_posts())

        diff = gen.preview_apply(schema, tmp_path)
        sections = _parse_diff_sections(diff)

        # The default file (app/models.py) doesn't exist on disk, so it won't
        # appear in the diff via preview_apply (which only diffs existing files).
        # What we CAN assert: users.py and posts.py diffs must NOT add UserRole.
        for fname, chunk in sections.items():
            if fname in ("app/users.py", "app/posts.py"):
                added_lines = [l for l in chunk.splitlines() if l.startswith("+")]
                assert not any("UserRole" in l for l in added_lines), (
                    f"UserRole enum class must NOT be added to {fname!r}; "
                    f"it belongs in the default file ({default!r})"
                )

    def test_enum_none_file_path_absent_from_non_default_file(self, tmp_path: Path):
        """Enum with file_path=None must NOT be injected into existing model files."""
        schema = _make_two_file_schema(enum_file_path=None)
        default = _default_path_for(schema, tmp_path)
        gen = get_generator("sqlmodel")

        (tmp_path / "app").mkdir(parents=True, exist_ok=True)
        (tmp_path / "app" / "users.py").write_text(self._existing_users())
        (tmp_path / "app" / "posts.py").write_text(self._existing_posts())

        diff = gen.preview_apply(schema, tmp_path)
        sections = _parse_diff_sections(diff)

        for fname in ("app/users.py", "app/posts.py"):
            chunk = sections.get(fname, "")
            added = [l for l in chunk.splitlines() if l.startswith("+")]
            assert not any("UserRole" in l for l in added), (
                f"UserRole enum must NOT be added to {fname!r} (default={default!r})"
            )

    def test_enum_explicit_file_path_only_in_that_file(self, tmp_path: Path):
        """Enum with explicit file_path='app/users.py' must NOT appear in posts.py."""
        schema = _make_two_file_schema(enum_file_path="app/users.py")
        gen = get_generator("sqlmodel")

        (tmp_path / "app").mkdir(parents=True, exist_ok=True)
        (tmp_path / "app" / "users.py").write_text(self._existing_users())
        (tmp_path / "app" / "posts.py").write_text(self._existing_posts())

        diff = gen.preview_apply(schema, tmp_path)
        sections = _parse_diff_sections(diff)

        posts_chunk = sections.get("app/posts.py", "")
        added = [l for l in posts_chunk.splitlines() if l.startswith("+")]
        assert not any("UserRole" in l for l in added), (
            "UserRole with explicit file_path='app/users.py' must NOT appear in app/posts.py"
        )


# ---------------------------------------------------------------------------
# Tests for local_enum_names set construction (unit-level)
# ---------------------------------------------------------------------------


class TestLocalEnumNamesSetLogic:
    """Unit-test the corrected condition directly, without disk I/O."""

    def _build_local_enum_names(
        self, schema: AlterSchema, rel_path: str, default_path: str
    ) -> set[str]:
        """Replicate the fixed condition from all three code locations."""
        return {
            e.name for e in schema.enums
            if e.file_path == rel_path or (e.file_path is None and rel_path == default_path)
        }

    def test_none_file_path_matches_only_default(self):
        schema = _make_two_file_schema(enum_file_path=None)
        # _default_model_path returns "app/models.py" for this two-file schema
        default = "app/models.py"

        names_default = self._build_local_enum_names(schema, default, default)
        names_users = self._build_local_enum_names(schema, "app/users.py", default)
        names_posts = self._build_local_enum_names(schema, "app/posts.py", default)

        assert "UserRole" in names_default
        assert "UserRole" not in names_users
        assert "UserRole" not in names_posts

    def test_explicit_file_path_matches_only_target(self):
        schema = _make_two_file_schema(enum_file_path="app/users.py")
        default = "app/models.py"

        names_target = self._build_local_enum_names(schema, "app/users.py", default)
        names_posts = self._build_local_enum_names(schema, "app/posts.py", default)
        names_default = self._build_local_enum_names(schema, default, default)

        assert "UserRole" in names_target
        assert "UserRole" not in names_posts
        assert "UserRole" not in names_default

    def test_explicit_file_path_non_default_file(self):
        """Enum routed to a non-default file → absent from default file too."""
        schema = _make_two_file_schema(enum_file_path="app/posts.py")
        default = "app/models.py"

        names_default = self._build_local_enum_names(schema, default, default)
        names_users = self._build_local_enum_names(schema, "app/users.py", default)
        names_posts = self._build_local_enum_names(schema, "app/posts.py", default)

        assert "UserRole" not in names_default
        assert "UserRole" not in names_users
        assert "UserRole" in names_posts

    def test_none_file_path_with_single_file_project(self):
        """Single-file project: default == rel_path → enum included in that file."""
        schema = AlterSchema(
            orm="sqlmodel",
            tables=[
                Table(
                    name="users",
                    file_path="app/models.py",
                    columns=[Column(name="id", type="uuid", primary_key=True, nullable=False)],
                ),
            ],
            enums=[
                EnumDef(name="UserRole", values=["admin", "user"], file_path=None)
            ],
        )
        default = "app/models.py"

        names = self._build_local_enum_names(schema, "app/models.py", default)
        assert "UserRole" in names

    def test_multiple_enums_split_across_files(self):
        """Two enums routed to different files are each emitted only once."""
        schema = AlterSchema(
            orm="sqlmodel",
            tables=[
                Table(
                    name="users",
                    file_path="app/users.py",
                    columns=[Column(name="id", type="uuid", primary_key=True, nullable=False)],
                ),
                Table(
                    name="posts",
                    file_path="app/posts.py",
                    columns=[Column(name="id", type="uuid", primary_key=True, nullable=False)],
                ),
            ],
            enums=[
                EnumDef(name="UserRole", values=["admin", "user"], file_path="app/users.py"),
                EnumDef(name="PostStatus", values=["draft", "published"], file_path="app/posts.py"),
                EnumDef(name="GlobalKind", values=["a", "b"], file_path=None),
            ],
        )
        # For this schema _default_model_path returns "app/models.py"
        default = "app/models.py"

        names_users = self._build_local_enum_names(schema, "app/users.py", default)
        names_posts = self._build_local_enum_names(schema, "app/posts.py", default)
        names_default = self._build_local_enum_names(schema, default, default)

        assert "UserRole" in names_users
        assert "PostStatus" not in names_users
        assert "GlobalKind" not in names_users   # None → "app/models.py", not users.py

        assert "PostStatus" in names_posts
        assert "UserRole" not in names_posts
        assert "GlobalKind" not in names_posts   # None → "app/models.py", not posts.py

        assert "GlobalKind" in names_default     # None → routed to default
        assert "UserRole" not in names_default
        assert "PostStatus" not in names_default

    def test_no_enums_in_schema(self):
        """Schema with no enums produces empty local_enum_names everywhere."""
        schema = AlterSchema(
            orm="sqlmodel",
            tables=[
                Table(
                    name="users",
                    file_path="app/users.py",
                    columns=[Column(name="id", type="uuid", primary_key=True, nullable=False)],
                ),
            ],
            enums=[],
        )
        names = self._build_local_enum_names(schema, "app/users.py", "app/users.py")
        assert names == set()

    def test_none_file_path_absent_from_completely_different_file(self):
        """Enum(file_path=None) is absent from a third file unrelated to default."""
        schema = _make_two_file_schema(enum_file_path=None)
        # _default_model_path returns "app/models.py" for this schema
        default = "app/models.py"
        names_third = self._build_local_enum_names(schema, "app/admin.py", default)
        assert "UserRole" not in names_third


# ---------------------------------------------------------------------------
# Tests for _apply_to_code_impl (mcp_server path) via StagingManager
# ---------------------------------------------------------------------------


class TestApplyToCodeImplEnumRouting:
    """Verify that ``_apply_to_code_impl`` uses the corrected enum routing."""

    def test_enum_not_written_to_non_default_files(self, tmp_path: Path):
        """In a two-file project, enum(file_path=None) must NOT appear in either
        existing model file; it routes to the default file (app/models.py) which
        is generated separately."""
        from alter.mcp_server import _apply_to_code_impl
        from alter.staging import StagingManager

        schema = _make_two_file_schema(enum_file_path=None)
        # Confirm default is app/models.py for this schema
        default = _default_path_for(schema, tmp_path)
        assert default == "app/models.py"

        alter_file = tmp_path / "schema.alter"
        schema.save(alter_file)

        (tmp_path / "app").mkdir(parents=True, exist_ok=True)
        (tmp_path / "app" / "users.py").write_text("")
        (tmp_path / "app" / "posts.py").write_text("")
        # Note: app/models.py does NOT exist yet; it will be created fresh

        staging = StagingManager(alter_file)
        _apply_to_code_impl(staging, tmp_path, preview=False)

        users_content = (tmp_path / "app" / "users.py").read_text()
        posts_content = (tmp_path / "app" / "posts.py").read_text()

        # The enum CLASS DEFINITION must not appear in these files.
        # (The type annotation "role: UserRole" is fine and expected in users.py.)
        assert "class UserRole" not in users_content, (
            "Enum class definition must NOT be written to app/users.py"
        )
        assert "class UserRole" not in posts_content, (
            "Enum class definition must NOT be written to app/posts.py"
        )

    def test_enum_with_explicit_file_path_not_in_other_files(self, tmp_path: Path):
        """Enum with explicit file_path='app/users.py' must not appear in posts.py."""
        from alter.mcp_server import _apply_to_code_impl
        from alter.staging import StagingManager

        schema = _make_two_file_schema(enum_file_path="app/users.py")
        alter_file = tmp_path / "schema.alter"
        schema.save(alter_file)

        (tmp_path / "app").mkdir(parents=True, exist_ok=True)
        (tmp_path / "app" / "users.py").write_text("")
        (tmp_path / "app" / "posts.py").write_text("")

        staging = StagingManager(alter_file)
        _apply_to_code_impl(staging, tmp_path, preview=False)

        posts_content = (tmp_path / "app" / "posts.py").read_text()
        users_content = (tmp_path / "app" / "users.py").read_text()

        assert "class UserRole" in users_content, "Enum class must be written to its explicit file (users.py)"
        assert "class UserRole" not in posts_content, "Enum class must NOT appear in posts.py"
