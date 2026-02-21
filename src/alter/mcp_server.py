"""Alter MCP server.

Exposes all schema management tools to any AI assistant that speaks the
Model Context Protocol (stdio transport).

Start via::

    alter mcp [--file schema.alter]

The server holds a ``StagingManager`` singleton that mirrors the canvas
staging model: current_schema (on disk) + proposed_schema (in-memory).
All ``Schema Tools`` modify the *proposed* schema — nothing touches the disk
until ``commit_changes`` is called.

Tools are organised into four groups:

Schema Tools (modify proposed, never disk)
    read_schema, read_proposed, add_table, add_column, modify_column,
    add_relation, remove_entity, rename_entity

Review Tools (read-only)
    get_diff, preview_migration, validate

Action Tools (may write to disk or execute)
    commit_changes, discard_changes, undo, redo,
    apply_to_code, sync_from_code,
    generate_migration, run_migration

Import / Export Tools
    import_schema, export_schema, diff_markdown, introspect_db

Resources
    alter://schema, alter://proposed, alter://models,
    alter://diff, alter://migration
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from alter.diff import diff_schemas
from alter.errors import AlterError
from alter.schema import AlterSchema, Column, EnumDef, Relation, Table
from alter.staging import StagingManager
from alter.validate import validate_schema

# ---------------------------------------------------------------------------
# Server singleton + state
# ---------------------------------------------------------------------------

mcp = FastMCP("Alter")

_staging: StagingManager | None = None
_path: Path | None = None


def init_mcp(alter_file_path: Path) -> None:
    """Initialise the MCP server with a .alter file path.

    Called by the CLI before ``mcp.run()``.
    """
    global _staging, _path
    _path = alter_file_path
    _staging = StagingManager(alter_file_path)


def _get_staging() -> StagingManager:
    if _staging is None:
        raise RuntimeError(
            "Alter MCP server is not initialised. "
            "Run via 'alter mcp' or call init_mcp(path) first."
        )
    return _staging


def _get_path() -> Path:
    if _path is None:
        raise RuntimeError("Alter MCP server path not set.")
    return _path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _schema_summary(schema: AlterSchema) -> dict[str, Any]:
    """Return a compact schema summary (tables, columns, relations)."""
    return {
        "orm": schema.orm,
        "tables": [
            {
                "name": t.name,
                "columns": [
                    {
                        "name": c.name,
                        "type": c.type,
                        "primary_key": c.primary_key,
                        "nullable": c.nullable,
                        "unique": c.unique,
                        "foreign_key": c.foreign_key,
                        "default": c.default,
                    }
                    for c in t.columns
                ],
            }
            for t in schema.tables
        ],
        "relations": [
            {
                "name": r.name,
                "from": f"{r.from_table}.{r.from_column}",
                "to": f"{r.to_table}.{r.to_column}",
                "type": r.type,
                "on_delete": r.on_delete,
            }
            for r in schema.relations
        ],
    }


def _diff_markdown_text(staging: StagingManager) -> str:
    """Build a human-readable markdown changelog of pending changes."""
    if not staging.has_pending():
        return "_No pending changes._"

    changes = staging.get_diff()
    if not changes:
        return "_No pending changes._"

    sections: dict[str, list[str]] = {
        "Added Tables": [],
        "Dropped Tables": [],
        "Added Columns": [],
        "Dropped Columns": [],
        "Modified Columns": [],
        "Added Relations": [],
        "Dropped Relations": [],
    }

    for ch in changes:
        if ch.type == "add_table":
            sections["Added Tables"].append(f"- `{ch.table}`")
        elif ch.type == "drop_table":
            sections["Dropped Tables"].append(f"- ~~`{ch.table}`~~ ⚠️ destructive")
        elif ch.type == "add_column":
            sections["Added Columns"].append(f"- `{ch.table}.{ch.column}`")
        elif ch.type == "drop_column":
            sections["Dropped Columns"].append(f"- ~~`{ch.table}.{ch.column}`~~ ⚠️ destructive")
        elif ch.type == "modify_column":
            details = ", ".join(
                f"{k}: `{v[0]}` → `{v[1]}`" for k, v in ch.details.items()
            )
            sections["Modified Columns"].append(
                f"- `{ch.table}.{ch.column}` ({details})"
                + (" ⚠️ destructive" if ch.destructive else "")
            )
        elif ch.type == "add_relation":
            sections["Added Relations"].append(
                f"- `{ch.table}.{ch.column}` → `{ch.details.get('to', '?')}`"
            )
        elif ch.type == "drop_relation":
            sections["Dropped Relations"].append(
                f"- ~~`{ch.table}.{ch.column}`~~ ⚠️ destructive"
            )

    lines = ["## Schema Changes\n"]
    for heading, items in sections.items():
        if items:
            lines.append(f"### {heading}")
            lines.extend(items)
            lines.append("")

    return "\n".join(lines)


def _apply_to_code_impl(
    staging: StagingManager, project_root: Path, preview: bool = False
) -> str:
    """Write (or diff) committed schema to ORM model files."""
    from alter.generators.base import get_generator  # noqa: PLC0415

    schema = staging.current_schema
    gen = get_generator(schema.orm)

    # Group tables by file_path
    file_groups: dict[str, list] = {}
    for t in schema.tables:
        fp = t.file_path or "app/models.py"
        file_groups.setdefault(fp, []).append(t)

    diffs: list[str] = []
    writes: list[str] = []

    for rel_path, tables in file_groups.items():
        abs_path = project_root / rel_path
        # Build a per-file sub-schema for the generator (keep all enums for type resolution)
        file_schema = schema.model_copy(update={"tables": tables})
        # Only define enum classes that are owned by this file; others are imported elsewhere
        local_enum_names = {
            e.name for e in schema.enums
            if e.file_path is None or e.file_path == rel_path
        }

        if abs_path.exists():
            existing = abs_path.read_text()
            updated = gen.update_models(file_schema, existing, local_enum_names=local_enum_names)
        else:
            existing = ""
            updated = gen.generate_models(file_schema, local_enum_names=local_enum_names)

        if updated == existing:
            continue

        if preview:
            import difflib  # noqa: PLC0415
            diff = "\n".join(
                difflib.unified_diff(
                    existing.splitlines(),
                    updated.splitlines(),
                    fromfile=f"a/{rel_path}",
                    tofile=f"b/{rel_path}",
                    lineterm="",
                )
            )
            diffs.append(diff)
        else:
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_text(updated)
            writes.append(rel_path)

    if preview:
        return "\n\n".join(diffs) if diffs else "No changes — files are already up to date."
    return (
        "Applied to: " + ", ".join(writes)
        if writes
        else "No changes — files are already up to date."
    )


def _sync_from_code_impl(
    staging: StagingManager, project_root: Path, alter_file: Path | None = None
) -> str:
    """Parse ORM model files and update current_schema (preserving positions).

    ``alter_file`` is the path to write the updated schema to.  When called
    from the canvas server or tests the caller passes the path explicitly.
    When called from the MCP tool wrapper the default ``None`` falls back to
    the module-level singleton via ``_get_path()``.
    """
    from alter.parsers.base import get_parser  # noqa: PLC0415

    schema = staging.current_schema
    parser = get_parser(schema.orm, project_root=project_root)

    # Collect all unique model file paths referenced in the schema.
    # Include enum file_path entries so that enum-only files (e.g. app/enums.py)
    # are re-parsed when they change even if no table file directly imports them.
    file_paths: set[str] = {t.file_path for t in schema.tables if t.file_path}
    file_paths.update(e.file_path for e in schema.enums if e.file_path)
    if not file_paths:
        # Fall back to scanning the project root directory
        result = parser.parse_directory(project_root)
    else:
        # Parse only the files we know about, collecting tables, enums AND relations.
        # parse_file_result() is used (not parse_file) so that custom enum
        # types referenced by columns survive the schema validation step.
        all_tables = []
        all_enums: list = []
        all_relations: list = []
        seen_enum_names: set[str] = set()
        seen_rel_keys: set[tuple] = set()
        skipped: list[Path] = []
        for rel_path in sorted(file_paths):
            abs_path = project_root / rel_path
            if abs_path.exists():
                try:
                    file_result = parser.parse_file_result(abs_path)
                    all_tables.extend(file_result.schema.tables)
                    # Deduplicate enums: same name may appear in multiple file parses
                    # (e.g. app/enums.py enums also appear when parsing starter.py
                    # because parse_file_result follows imports transitively).
                    for e in file_result.schema.enums:
                        if e.name not in seen_enum_names:
                            all_enums.append(e)
                            seen_enum_names.add(e.name)
                    # Collect relations; deduplicate by (from_table, from_column)
                    for r in file_result.schema.relations:
                        key = (r.from_table, r.from_column)
                        if key not in seen_rel_keys:
                            all_relations.append(r)
                            seen_rel_keys.add(key)
                except Exception:
                    skipped.append(abs_path)
        from alter.parsers.base import ParseResult  # noqa: PLC0415
        partial = AlterSchema(
            orm=schema.orm,
            tables=all_tables,
            enums=all_enums,
            relations=all_relations,
        )
        result = ParseResult(schema=partial, skipped_files=skipped)

    # Preserve positions from the current schema
    pos_map = {t.name: t.position for t in schema.tables}
    for t in result.schema.tables:
        if t.name in pos_map:
            t.position = pos_map[t.name]

    # Update and persist
    staging.current_schema = result.schema
    save_path = alter_file if alter_file is not None else _get_path()
    staging.current_schema.save(save_path)

    summary = f"Synced {len(result.schema.tables)} tables"
    if result.skipped_files:
        summary += f" (skipped {len(result.skipped_files)} files)"
    return summary


# ---------------------------------------------------------------------------
# Schema Tools — modify proposed, never disk
# ---------------------------------------------------------------------------


@mcp.tool()
def read_schema() -> dict[str, Any]:
    """Return the current committed schema (what's on disk)."""
    return _schema_summary(_get_staging().current_schema)


@mcp.tool()
def read_proposed() -> dict[str, Any]:
    """Return the proposed (staged) schema, or the current schema if no changes are pending."""
    s = _get_staging()
    return _schema_summary(s.proposed_schema if s.has_pending() else s.current_schema)


@mcp.tool()
def add_table(name: str, file_path: str = "app/models.py") -> str:
    """Add a new table to the proposed schema.

    A default ``id uuid PRIMARY KEY`` column is seeded automatically.
    Use ``add_column`` to add further columns.

    Args:
        name:      Table name (snake_case).
        file_path: Relative path to the ORM model file (default app/models.py).
    """
    staging = _get_staging()

    def apply(schema: AlterSchema) -> AlterSchema:
        s = copy.deepcopy(schema)
        if any(t.name == name for t in s.tables):
            raise ValueError(f"Table '{name}' already exists")
        tbl = Table(name=name, file_path=file_path)
        tbl.columns.append(
            Column(name="id", type="uuid", primary_key=True, nullable=False, default="uuid4")
        )
        s.tables.append(tbl)
        return s

    try:
        staging.propose(apply)
        return f"Added table '{name}' with default id column."
    except (AlterError, ValueError) as exc:
        return f"Error: {exc}"


@mcp.tool()
def add_file(file_path: str) -> str:
    """Add tables from a model file to the schema.

    Use when alter init missed a file, or the user mentions models in a
    non-standard location. Parses the file and adds any new tables.

    Args:
        file_path: Path to the model file, relative to the project root.
    """
    from alter.parsers.base import get_parser

    staging = _get_staging()
    project_root = _get_path().parent
    abs_path = (project_root / file_path).resolve()

    if not abs_path.exists():
        return f"Error: file not found: {file_path}"

    parser = get_parser(staging.current_schema.orm, project_root=project_root)
    if not parser.detect_orm(abs_path):
        return f"Error: {file_path} does not contain {staging.current_schema.orm} models."

    try:
        # Use parse_file_result to capture enum definitions too — custom enum
        # types on columns would fail schema validation if enums are not added.
        file_result = parser.parse_file_result(abs_path)
    except Exception as exc:
        return f"Error parsing {file_path}: {exc}"

    tables = file_result.schema.tables
    enums = file_result.schema.enums

    if not tables:
        return f"No tables found in {file_path}."

    rel_path = str(abs_path.relative_to(project_root))

    def apply(schema: AlterSchema) -> AlterSchema:
        s = copy.deepcopy(schema)
        existing_tables = {t.name for t in s.tables}
        existing_enums = {e.name for e in s.enums}
        added = []
        for tbl in tables:
            if tbl.name not in existing_tables:
                tbl.file_path = rel_path
                s.tables.append(copy.deepcopy(tbl))
                added.append(tbl.name)
        # Always merge new enum definitions (idempotent — skip if already present)
        for enum in enums:
            if enum.name not in existing_enums:
                s.enums.append(copy.deepcopy(enum))
        if not added:
            raise ValueError(f"All tables from {file_path} already exist in schema.")
        return s

    existing_names = {t.name for t in staging.current_schema.tables}
    try:
        staging.propose(apply)
        new_tables = [t for t in tables if t.name not in existing_names]
        return f"Added {len(new_tables)} table(s) from {rel_path}: {', '.join(t.name for t in new_tables)}"
    except (AlterError, ValueError) as exc:
        return f"Error: {exc}"


@mcp.tool()
def add_column(
    table: str,
    name: str,
    type: str,
    nullable: bool = True,
    unique: bool = False,
    primary_key: bool = False,
    default: str | None = None,
    max_length: int | None = None,
    foreign_key: str | None = None,
) -> str:
    """Add a column to a table in the proposed schema.

    Args:
        table:       Target table name.
        name:        Column name.
        type:        Alter type: uuid, string, text, int, bigint, float, decimal,
                     bool, datetime, date, time, json, bytes.
        nullable:    Allow NULLs (default True).
        unique:      Add UNIQUE constraint (default False).
        primary_key: Mark as primary key (default False).
        default:     Default value: uuid4, now, true, false, or a literal.
        max_length:  For string columns — max character length.
        foreign_key: FK reference in 'table.column' format (optional).
    """
    staging = _get_staging()

    def apply(schema: AlterSchema) -> AlterSchema:
        s = copy.deepcopy(schema)
        tbl = next((t for t in s.tables if t.name == table), None)
        if tbl is None:
            raise ValueError(f"Table '{table}' not found")
        if any(c.name == name for c in tbl.columns):
            raise ValueError(f"Column '{name}' already exists in '{table}'")
        tbl.columns.append(
            Column(
                name=name,
                type=type,
                nullable=nullable if not primary_key else False,
                unique=unique,
                primary_key=primary_key,
                default=default,
                max_length=max_length,
                foreign_key=foreign_key,
            )
        )
        return s

    try:
        staging.propose(apply)
        return f"Added column '{name}' to table '{table}'."
    except (AlterError, ValueError) as exc:
        return f"Error: {exc}"


@mcp.tool()
def modify_column(
    table: str,
    column: str,
    new_name: str | None = None,
    new_type: str | None = None,
    nullable: bool | None = None,
    unique: bool | None = None,
    default: str | None = None,
    max_length: int | None = None,
) -> str:
    """Modify properties of an existing column in the proposed schema.

    Only the provided fields are changed; omit a field to leave it unchanged.
    """
    staging = _get_staging()

    def apply(schema: AlterSchema) -> AlterSchema:
        s = copy.deepcopy(schema)
        tbl = next((t for t in s.tables if t.name == table), None)
        if tbl is None:
            raise ValueError(f"Table '{table}' not found")
        col = next((c for c in tbl.columns if c.name == column), None)
        if col is None:
            raise ValueError(f"Column '{column}' not found in '{table}'")
        if new_name is not None:
            col.name = new_name
        if new_type is not None:
            col.type = new_type
        if nullable is not None:
            col.nullable = nullable
        if unique is not None:
            col.unique = unique
        if default is not None:
            col.default = default
        if max_length is not None:
            col.max_length = max_length
        return s

    try:
        staging.propose(apply)
        return f"Modified column '{column}' in table '{table}'."
    except (AlterError, ValueError) as exc:
        return f"Error: {exc}"


@mcp.tool()
def add_relation(
    from_table: str,
    from_column: str,
    to_table: str,
    to_column: str,
    relation_type: str = "many-to-one",
    on_delete: str = "CASCADE",
) -> str:
    """Add a foreign key relation to the proposed schema.

    Args:
        from_table:    Table that holds the FK column.
        from_column:   FK column name.
        to_table:      Referenced table.
        to_column:     Referenced column.
        relation_type: one-to-one, one-to-many, many-to-one (default), many-to-many.
        on_delete:     CASCADE (default), SET NULL, RESTRICT, NO ACTION, SET DEFAULT.
    """
    staging = _get_staging()

    def apply(schema: AlterSchema) -> AlterSchema:
        s = copy.deepcopy(schema)
        table_names = {t.name for t in s.tables}
        if from_table not in table_names:
            raise ValueError(f"Table '{from_table}' not found")
        if to_table not in table_names:
            raise ValueError(f"Table '{to_table}' not found")
        rel = Relation(
            name=f"{from_table}_{from_column}_{to_table}_fkey",
            from_table=from_table,
            from_column=from_column,
            to_table=to_table,
            to_column=to_column,
            type=relation_type,  # type: ignore[arg-type]
            on_delete=on_delete,  # type: ignore[arg-type]
        )
        s.relations.append(rel)
        return s

    try:
        staging.propose(apply)
        return f"Added relation {from_table}.{from_column} → {to_table}.{to_column}."
    except (AlterError, ValueError) as exc:
        return f"Error: {exc}"


@mcp.tool()
def remove_entity(table: str, column: str | None = None) -> str:
    """Drop a table or column from the proposed schema.

    If ``column`` is provided, drops only that column.
    If ``column`` is omitted, drops the entire table (and its relations).

    ⚠️ This is a destructive operation — data will be lost on next migration.
    """
    staging = _get_staging()

    def apply(schema: AlterSchema) -> AlterSchema:
        s = copy.deepcopy(schema)
        if column is None:
            # Drop table
            if not any(t.name == table for t in s.tables):
                raise ValueError(f"Table '{table}' not found")
            s.tables = [t for t in s.tables if t.name != table]
            s.relations = [
                r for r in s.relations if r.from_table != table and r.to_table != table
            ]
        else:
            # Drop column
            tbl = next((t for t in s.tables if t.name == table), None)
            if tbl is None:
                raise ValueError(f"Table '{table}' not found")
            if not any(c.name == column for c in tbl.columns):
                raise ValueError(f"Column '{column}' not found in '{table}'")
            tbl.columns = [c for c in tbl.columns if c.name != column]
        return s

    try:
        staging.propose(apply)
        target = f"'{table}.{column}'" if column else f"table '{table}'"
        return f"Dropped {target} from proposed schema."
    except (AlterError, ValueError) as exc:
        return f"Error: {exc}"


@mcp.tool()
def rename_entity(table: str, new_name: str, column: str | None = None) -> str:
    """Rename a table or column in the proposed schema.

    If ``column`` is provided, renames that column to ``new_name``.
    If ``column`` is omitted, renames the table itself.
    """
    staging = _get_staging()

    def apply(schema: AlterSchema) -> AlterSchema:
        s = copy.deepcopy(schema)
        if column is None:
            # Rename table
            tbl = next((t for t in s.tables if t.name == table), None)
            if tbl is None:
                raise ValueError(f"Table '{table}' not found")
            if any(t.name == new_name for t in s.tables):
                raise ValueError(f"Table '{new_name}' already exists")
            old_name = tbl.name
            tbl.name = new_name
            # Update FK references in relations
            for rel in s.relations:
                if rel.from_table == old_name:
                    rel.from_table = new_name
                if rel.to_table == old_name:
                    rel.to_table = new_name
        else:
            # Rename column
            tbl = next((t for t in s.tables if t.name == table), None)
            if tbl is None:
                raise ValueError(f"Table '{table}' not found")
            col = next((c for c in tbl.columns if c.name == column), None)
            if col is None:
                raise ValueError(f"Column '{column}' not found in '{table}'")
            if any(c.name == new_name for c in tbl.columns):
                raise ValueError(f"Column '{new_name}' already exists in '{table}'")
            col.name = new_name
            # Update FK references
            for rel in s.relations:
                if rel.from_table == table and rel.from_column == column:
                    rel.from_column = new_name
                if rel.to_table == table and rel.to_column == column:
                    rel.to_column = new_name
        return s

    try:
        staging.propose(apply)
        target = f"'{table}.{column}'" if column else f"table '{table}'"
        return f"Renamed {target} → '{new_name}'."
    except (AlterError, ValueError) as exc:
        return f"Error: {exc}"


# ---------------------------------------------------------------------------
# Review Tools — read-only
# ---------------------------------------------------------------------------


@mcp.tool()
def get_diff() -> list[dict[str, Any]]:
    """Return the diff between the current and proposed schemas.

    Returns an empty list if there are no pending changes.
    """
    staging = _get_staging()
    changes = staging.get_diff()
    return [
        {
            "type": c.type,
            "table": c.table,
            "column": c.column,
            "destructive": c.destructive,
            "details": c.details,
        }
        for c in changes
    ]


@mcp.tool()
def preview_migration() -> str:
    """Return the SQL that WOULD run for the pending changes.

    Does NOT write any files or touch the database.
    Returns an empty string if there are no pending changes.
    Copy this SQL into your migration manager (Alembic, Django, Flyway, raw SQL, etc.).
    """
    from alter.canvas.server import _migration_sql  # noqa: PLC0415

    return _migration_sql(_get_staging())


@mcp.tool()
def validate() -> list[dict[str, Any]]:
    """Validate the proposed (or current) schema and return any issues.

    Issues have a ``severity`` of ``"error"``, ``"warning"``, or ``"info"``.
    Errors must be resolved before committing.
    """
    staging = _get_staging()
    schema = staging.proposed_schema if staging.has_pending() else staging.current_schema
    issues = validate_schema(schema)
    return [
        {
            "severity": i.severity,
            "table": i.table,
            "column": i.column,
            "message": i.message,
        }
        for i in issues
    ]


# ---------------------------------------------------------------------------
# Action Tools — may write to disk
# ---------------------------------------------------------------------------


@mcp.tool()
def commit_changes() -> str:
    """Commit the proposed schema to disk.

    Writes the .alter file.  If the canvas is open it will update via SSE.
    Clears the undo/redo stacks.
    """
    staging = _get_staging()
    if not staging.has_pending():
        return "Nothing to commit — no pending changes."
    n = len(staging.get_diff())
    staging.commit()
    return f"Committed {n} change{'s' if n != 1 else ''} to {_get_path().name}."


@mcp.tool()
def discard_changes() -> str:
    """Discard all pending proposed changes and clear the undo/redo stacks."""
    staging = _get_staging()
    if not staging.has_pending():
        return "Nothing to discard — no pending changes."
    staging.discard()
    return "Discarded all pending changes."


@mcp.tool()
def undo() -> str:
    """Undo the most recent schema proposal."""
    staging = _get_staging()
    result = staging.undo()
    if result is None:
        return "Nothing to undo."
    return "Undid last change."


@mcp.tool()
def redo() -> str:
    """Re-apply the most recently undone schema proposal."""
    staging = _get_staging()
    result = staging.redo()
    if result is None:
        return "Nothing to redo."
    return "Redid last undone change."


@mcp.tool()
def apply_to_code(preview: bool = False) -> str:
    """Write the committed schema to ORM model files.

    Uses surgical update — only modified classes are changed, everything else
    (comments, helpers, custom methods) is preserved.

    Args:
        preview: If True, return a unified diff without writing any files.
    """
    staging = _get_staging()
    project_root = _get_path().parent
    try:
        return _apply_to_code_impl(staging, project_root, preview=preview)
    except (AlterError, Exception) as exc:
        return f"Error: {exc}"


@mcp.tool()
def sync_from_code() -> str:
    """Read ORM model files and update the .alter schema.

    Preserves existing table positions on the canvas.
    """
    staging = _get_staging()
    project_root = _get_path().parent
    try:
        return _sync_from_code_impl(staging, project_root)
    except (AlterError, Exception) as exc:
        return f"Error: {exc}"


# ---------------------------------------------------------------------------
# Import / Export Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def import_schema(source: str, format: str = "sql") -> str:
    """Import a schema from SQL DDL or a .alter file into the proposed schema.

    Tables already present in the schema are skipped (no overwrites).

    Args:
        source: Path to a .sql or .alter file, OR raw SQL DDL text.
        format: ``"sql"`` (default) or ``"alter"``.
    """
    staging = _get_staging()

    try:
        if format == "alter":
            from alter.importers.alter_file import import_alter_file  # noqa: PLC0415

            src_path = Path(source)
            if not src_path.exists():
                return f"Error: file not found: {source}"
            imported = import_alter_file(src_path)
        else:
            from alter.importers.sql import import_sql  # noqa: PLC0415

            # Accept a file path or raw SQL text
            src_path = Path(source)
            sql_text = src_path.read_text() if src_path.exists() else source
            imported = import_sql(sql_text, orm=staging.current_schema.orm)

    except (AlterError, Exception) as exc:
        return f"Error importing schema: {exc}"

    def apply(schema: AlterSchema) -> AlterSchema:
        s = copy.deepcopy(schema)
        existing_names = {t.name for t in s.tables}
        added = 0
        for tbl in imported.tables:
            if tbl.name not in existing_names:
                s.tables.append(copy.deepcopy(tbl))
                added += 1
        existing_rels = {(r.from_table, r.from_column) for r in s.relations}
        for rel in imported.relations:
            if (rel.from_table, rel.from_column) not in existing_rels:
                s.relations.append(copy.deepcopy(rel))
        return s

    staging.propose(apply)
    return f"Imported {len(imported.tables)} tables from {format.upper()}."


@mcp.tool()
def export_schema(format: str = "sql", proposed: bool = False) -> str:
    """Export the schema as SQL DDL, Mermaid ERD, or .alter JSON.

    Args:
        format:   ``"sql"`` (default), ``"mermaid"``, or ``"alter"``.
        proposed: If True, export the proposed (staged) schema instead of
                  the committed schema.
    """
    staging = _get_staging()
    schema = staging.proposed_schema if (proposed and staging.has_pending()) else staging.current_schema

    try:
        if format == "mermaid":
            from alter.exporters.mermaid import export_mermaid  # noqa: PLC0415
            return export_mermaid(schema)
        elif format == "alter":
            return schema.model_dump_json(indent=2)
        else:
            from alter.exporters.sql import export_sql  # noqa: PLC0415
            return export_sql(schema)
    except (AlterError, Exception) as exc:
        return f"Error exporting schema: {exc}"


@mcp.tool()
def diff_markdown() -> str:
    """Return a human-readable markdown summary of pending changes.

    Suitable for copying into a PR description.
    """
    return _diff_markdown_text(_get_staging())


@mcp.tool()
def introspect_db(connection_string: str | None = None) -> str:
    """Import the schema from a live PostgreSQL database into the proposed schema.

    Tables already present are skipped.

    Args:
        connection_string: A libpq connection string or URL.  Defaults to the
            ``DATABASE_URL`` environment variable if not provided.
    """
    cs = connection_string or os.environ.get("DATABASE_URL")
    if not cs:
        return (
            "Error: no connection string provided and DATABASE_URL is not set.\n"
            "Pass connection_string or set DATABASE_URL."
        )

    try:
        from alter.importers.database import import_from_database  # noqa: PLC0415

        imported = import_from_database(cs)
    except (ImportError, RuntimeError, Exception) as exc:
        return f"Error introspecting database: {exc}"

    staging = _get_staging()

    def apply(schema: AlterSchema) -> AlterSchema:
        s = copy.deepcopy(schema)
        existing_names = {t.name for t in s.tables}
        added = 0
        for tbl in imported.tables:
            if tbl.name not in existing_names:
                s.tables.append(copy.deepcopy(tbl))
                added += 1
        existing_rels = {(r.from_table, r.from_column) for r in s.relations}
        for rel in imported.relations:
            if (rel.from_table, rel.from_column) not in existing_rels:
                s.relations.append(copy.deepcopy(rel))
        return s

    staging.propose(apply)
    return f"Introspected {len(imported.tables)} tables from database."


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@mcp.resource("alter://schema")
def resource_schema() -> str:
    """The current committed .alter schema as JSON."""
    return _get_staging().current_schema.model_dump_json(indent=2)


@mcp.resource("alter://proposed")
def resource_proposed() -> str:
    """The proposed (staged) schema as JSON, or the current schema if none."""
    s = _get_staging()
    schema = s.proposed_schema if s.has_pending() else s.current_schema
    return schema.model_dump_json(indent=2)


@mcp.resource("alter://models")
def resource_models() -> str:
    """The current ORM model source code from disk."""
    staging = _get_staging()
    project_root = _get_path().parent
    parts: list[str] = []
    seen: set[str] = set()
    for tbl in staging.current_schema.tables:
        fp = tbl.file_path or "app/models.py"
        if fp not in seen:
            seen.add(fp)
            abs_path = project_root / fp
            if abs_path.exists():
                parts.append(f"# --- {fp} ---\n{abs_path.read_text()}")
    return "\n\n".join(parts) if parts else "No model files found."


@mcp.resource("alter://diff")
def resource_diff() -> str:
    """Changes between the current and proposed schemas as JSON."""
    changes = _get_staging().get_diff()
    return json.dumps(
        [
            {
                "type": c.type,
                "table": c.table,
                "column": c.column,
                "destructive": c.destructive,
            }
            for c in changes
        ],
        indent=2,
    )


@mcp.resource("alter://migration")
def resource_migration() -> str:
    """The SQL that would run for the pending changes."""
    from alter.canvas.server import _migration_sql  # noqa: PLC0415

    return _migration_sql(_get_staging())
