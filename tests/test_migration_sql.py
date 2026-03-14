"""Regression tests for _migration_sql in alter/canvas/server.py.

ISSUE: When a table that has FK relations is dropped, _migration_sql generated
both a DROP TABLE statement AND an ALTER TABLE … DROP CONSTRAINT statement for
the same table.  The ALTER TABLE is unreachable at runtime because the table no
longer exists after DROP TABLE.

Fix: _migration_sql now pre-computes ``dropped_tables`` from the change list
and skips redundant ALTER TABLE / DROP INDEX statements for any table that is
being dropped in the same migration batch.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from alter.canvas.server import _migration_sql
from alter.schema import AlterSchema, Column, Relation, Table
from alter.staging import StagingManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_staging(tmp_path: Path, schema: AlterSchema) -> StagingManager:
    """Persist *schema* and return a fresh StagingManager."""
    alter_file = tmp_path / "schema.alter"
    schema.save(alter_file)
    return StagingManager(alter_file)


def _post_comment_schema() -> AlterSchema:
    """Schema with post and comment tables; comment.post_id → post.id FK."""
    return AlterSchema(
        orm="sqlmodel",
        tables=[
            Table(
                name="post",
                file_path="app/models.py",
                columns=[
                    Column(name="id", type="uuid", primary_key=True, nullable=False),
                    Column(name="title", type="string", nullable=False),
                ],
            ),
            Table(
                name="comment",
                file_path="app/models.py",
                columns=[
                    Column(name="id", type="uuid", primary_key=True, nullable=False),
                    Column(name="post_id", type="uuid", nullable=False,
                           foreign_key="post.id"),
                    Column(name="body", type="text", nullable=False),
                ],
            ),
        ],
        relations=[
            Relation(
                name="fk_comment_post_id_post",
                from_table="comment",
                from_column="post_id",
                to_table="post",
                to_column="id",
                type="many-to-one",
                on_delete="CASCADE",
            )
        ],
    )


def _propose_drop_table(staging: StagingManager, table_name: str) -> None:
    """Propose dropping *table_name* (and its relations) from the schema."""
    def apply(schema: AlterSchema) -> AlterSchema:
        s = copy.deepcopy(schema)
        s.tables = [t for t in s.tables if t.name != table_name]
        s.relations = [
            r for r in s.relations
            if r.from_table != table_name and r.to_table != table_name
        ]
        return s
    staging.propose(apply)


# ---------------------------------------------------------------------------
# Core regression: drop_relation suppressed when table is also dropped
# ---------------------------------------------------------------------------


class TestDropTableSuppressesConstraintDrop:
    """ALTER TABLE … DROP CONSTRAINT must not appear for a dropped table."""

    def test_no_alter_table_drop_constraint_after_drop_table(self, tmp_path: Path):
        """Dropping comment table must not emit ALTER TABLE comment DROP CONSTRAINT."""
        schema = _post_comment_schema()
        staging = _make_staging(tmp_path, schema)
        _propose_drop_table(staging, "comment")

        sql = _migration_sql(staging)

        assert "DROP TABLE comment" in sql, "DROP TABLE comment must be present"
        assert "ALTER TABLE comment DROP CONSTRAINT" not in sql, (
            "ALTER TABLE comment DROP CONSTRAINT is unreachable after DROP TABLE"
        )

    def test_only_drop_table_statement_emitted_for_dropped_table(self, tmp_path: Path):
        """The only SQL referencing a dropped table is its DROP TABLE line."""
        schema = _post_comment_schema()
        staging = _make_staging(tmp_path, schema)
        _propose_drop_table(staging, "comment")

        sql = _migration_sql(staging)

        # Every line mentioning "comment" must be the DROP TABLE itself
        for line in sql.splitlines():
            if "comment" in line.lower():
                assert line.strip().upper().startswith("DROP TABLE"), (
                    f"Unexpected SQL line referencing dropped table 'comment': {line!r}"
                )

    def test_single_drop_table_statement(self, tmp_path: Path):
        """Exactly one DROP TABLE comment statement must be emitted."""
        schema = _post_comment_schema()
        staging = _make_staging(tmp_path, schema)
        _propose_drop_table(staging, "comment")

        sql = _migration_sql(staging)

        drop_lines = [l for l in sql.splitlines() if "DROP TABLE comment" in l]
        assert len(drop_lines) == 1, (
            f"Expected exactly one DROP TABLE comment; got {len(drop_lines)}"
        )


# ---------------------------------------------------------------------------
# drop_column suppressed for dropped table
# ---------------------------------------------------------------------------


class TestDropColumnSuppressedForDroppedTable:
    """ALTER TABLE … DROP COLUMN must not appear for a dropped table."""

    def test_no_drop_column_for_dropped_table(self, tmp_path: Path):
        """When a table is dropped, its individual DROP COLUMN changes are skipped."""
        # Start: comment table with 3 columns + post table
        schema = _post_comment_schema()
        staging = _make_staging(tmp_path, schema)

        # Propose: drop comment entirely (produces drop_table + drop_column diffs)
        _propose_drop_table(staging, "comment")

        sql = _migration_sql(staging)

        assert "ALTER TABLE comment DROP COLUMN" not in sql, (
            "ALTER TABLE comment DROP COLUMN must be suppressed when comment is being dropped"
        )


# ---------------------------------------------------------------------------
# drop_index suppressed for dropped table
# ---------------------------------------------------------------------------


class TestDropIndexSuppressedForDroppedTable:
    """DROP INDEX … must not appear for indexes on a dropped table."""

    def test_no_drop_index_for_dropped_table(self, tmp_path: Path):
        """Index drop change on a dropped table is suppressed."""
        schema = AlterSchema(
            orm="sqlmodel",
            tables=[
                Table(
                    name="post",
                    file_path="app/models.py",
                    columns=[
                        Column(name="id", type="uuid", primary_key=True, nullable=False),
                    ],
                ),
                Table(
                    name="comment",
                    file_path="app/models.py",
                    columns=[
                        Column(name="id", type="uuid", primary_key=True, nullable=False),
                        Column(name="slug", type="string", nullable=False, index=True),
                    ],
                ),
            ],
        )
        staging = _make_staging(tmp_path, schema)
        _propose_drop_table(staging, "comment")

        sql = _migration_sql(staging)

        assert "DROP INDEX" not in sql or "comment" not in sql, (
            "DROP INDEX for an index on the dropped table must be suppressed"
        )
        # More precise: no line should reference both DROP INDEX and comment
        for line in sql.splitlines():
            if "DROP INDEX" in line and "comment" in line:
                pytest.fail(
                    f"DROP INDEX referencing dropped table 'comment' must be suppressed: {line!r}"
                )


# ---------------------------------------------------------------------------
# Sanity: unrelated tables retain their constraint drops
# ---------------------------------------------------------------------------


class TestUnrelatedTableConstraintsKept:
    """Constraint drop SQL for tables that are NOT being dropped must still appear."""

    def test_constraint_drop_on_surviving_table_is_kept(self, tmp_path: Path):
        """Dropping a FK relation on a surviving table still emits ALTER TABLE."""
        schema = _post_comment_schema()
        staging = _make_staging(tmp_path, schema)

        # Propose: remove only the FK relation but keep both tables
        def drop_relation_only(s: AlterSchema) -> AlterSchema:
            sc = copy.deepcopy(s)
            sc.relations = []
            # Also clear the foreign_key field on the column
            for t in sc.tables:
                if t.name == "comment":
                    for col in t.columns:
                        if col.name == "post_id":
                            col.foreign_key = None
            return sc

        staging.propose(drop_relation_only)

        sql = _migration_sql(staging)

        assert "ALTER TABLE comment DROP CONSTRAINT" in sql, (
            "Constraint drop on a surviving table must still be emitted"
        )
        assert "DROP TABLE" not in sql, "No DROP TABLE should appear when only the relation is removed"

    def test_drop_unrelated_table_does_not_suppress_other_constraints(self, tmp_path: Path):
        """Dropping table A must not suppress constraint drops on table B."""
        schema = AlterSchema(
            orm="sqlmodel",
            tables=[
                Table(
                    name="post",
                    file_path="app/models.py",
                    columns=[
                        Column(name="id", type="uuid", primary_key=True, nullable=False),
                    ],
                ),
                Table(
                    name="comment",
                    file_path="app/models.py",
                    columns=[
                        Column(name="id", type="uuid", primary_key=True, nullable=False),
                        Column(name="post_id", type="uuid", nullable=False,
                               foreign_key="post.id"),
                    ],
                ),
                Table(
                    name="tag",
                    file_path="app/models.py",
                    columns=[
                        Column(name="id", type="uuid", primary_key=True, nullable=False),
                    ],
                ),
            ],
            relations=[
                Relation(
                    name="fk_comment_post_id_post",
                    from_table="comment",
                    from_column="post_id",
                    to_table="post",
                    to_column="id",
                    type="many-to-one",
                    on_delete="CASCADE",
                )
            ],
        )
        staging = _make_staging(tmp_path, schema)

        # Propose: drop `tag` table AND the comment→post FK relation
        def apply(s: AlterSchema) -> AlterSchema:
            sc = copy.deepcopy(s)
            sc.tables = [t for t in sc.tables if t.name != "tag"]
            sc.relations = []
            for t in sc.tables:
                if t.name == "comment":
                    for col in t.columns:
                        if col.name == "post_id":
                            col.foreign_key = None
            return sc

        staging.propose(apply)

        sql = _migration_sql(staging)

        # tag is dropped — only DROP TABLE tag
        assert "DROP TABLE tag" in sql
        assert "ALTER TABLE tag" not in sql

        # comment's FK constraint is still emitted (comment is NOT being dropped)
        assert "ALTER TABLE comment DROP CONSTRAINT" in sql


# ---------------------------------------------------------------------------
# Empty migration
# ---------------------------------------------------------------------------


class TestEmptyMigration:
    def test_no_pending_returns_empty_string(self, tmp_path: Path):
        """No pending changes → empty SQL output."""
        schema = _post_comment_schema()
        staging = _make_staging(tmp_path, schema)
        # No propose() call → no pending changes
        sql = _migration_sql(staging)
        assert sql == ""
