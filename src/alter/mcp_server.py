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

from alter.errors import AlterError
from alter.schema import AlterSchema, Column, Index, Relation, Table
from alter.staging import StagingManager
from alter.types import TYPE_MAP, is_enum_type
from alter.validate import validate_schema

# Sentinel for "parameter not supplied" — distinct from None so callers can
# explicitly pass None to clear optional string/int fields (e.g. default, max_length).
_UNSET = object()

# ---------------------------------------------------------------------------
# Server singleton + state
# ---------------------------------------------------------------------------


class _LazyMCP:
    """Proxy that buffers @mcp.tool() and @mcp.resource() registrations.

    All decorator calls at module import time are stored without touching
    FastMCP (which lives in ``mcp.server.fastmcp`` and is only available in
    ``mcp>=1.2.0``).  When ``init_mcp()`` creates the real FastMCP instance,
    ``_init_real()`` replays all registrations so that ``mcp.run()`` works.

    This lets ``canvas/server.py`` import ``_apply_to_code_impl`` and
    ``_sync_from_code_impl`` from this module without crashing on projects
    where ``mcp<1.2.0`` is installed as a project dependency.
    """

    def __init__(self) -> None:
        self._pending_tools: list[tuple[Any, dict[str, Any]]] = []
        self._pending_resources: list[tuple[Any, str, dict[str, Any]]] = []
        self._real: Any = None

    def tool(self, **kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self._pending_tools.append((fn, kwargs))
            return fn
        return decorator

    def resource(self, uri: str, **kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self._pending_resources.append((fn, uri, kwargs))
            return fn
        return decorator

    def _init_real(self, real_mcp: Any) -> None:
        self._real = real_mcp
        # MCP >=1.26 added structured_output which defaults to None, causing
        # FastMCP to generate Pydantic output models from return annotations.
        # On Python 3.11+ this can raise "A non-annotated attribute was
        # detected: result = <class 'str'>" and crash the server on startup.
        # Opt out by defaulting to False (tools return text content as before).
        import inspect as _inspect
        _structured_output_supported = (
            "structured_output" in _inspect.signature(real_mcp.tool).parameters
        )
        for fn, kwargs in self._pending_tools:
            kw = dict(kwargs)
            if _structured_output_supported:
                kw.setdefault("structured_output", False)
            real_mcp.tool(**kw)(fn)
        for fn, uri, kwargs in self._pending_resources:
            real_mcp.resource(uri, **kwargs)(fn)

    def run(self, **kwargs: Any) -> None:
        if self._real is None:
            raise RuntimeError("MCP server not initialised. Call init_mcp() first.")
        self._real.run(**kwargs)


mcp = _LazyMCP()

_staging: StagingManager | None = None
_path: Path | None = None


def init_mcp(alter_file_path: Path) -> None:
    """Initialise the MCP server with a .alter file path.

    Called by the CLI before ``mcp.run()``.  This is also where FastMCP is
    imported — the deferred import means projects with ``mcp<1.2.0`` can still
    run ``alter canvas`` without a crash.
    """
    global _staging, _path
    _path = alter_file_path
    _staging = StagingManager(alter_file_path)
    from mcp.server.fastmcp import FastMCP  # noqa: PLC0415
    from alter import __version__  # noqa: PLC0415
    real_mcp = FastMCP("Alter")
    real_mcp._mcp_server.version = __version__
    mcp._init_real(real_mcp)


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
                "indexes": [
                    {"columns": idx.columns, "unique": idx.unique}
                    for idx in t.indexes
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


def _validate_column_type(col_type: str, schema: AlterSchema) -> str | None:
    """Validate *col_type* against built-in types and schema enums.

    Returns ``None`` when the type is valid.  Returns a human-readable error
    string when the type is invalid so callers can surface it as a tool error.

    Valid types are:
    * Any key in ``TYPE_MAP`` (e.g. ``"string"``, ``"int"``, ``"uuid"``).
    * A PascalCase enum name that matches an ``EnumDef`` defined in *schema*.
    """
    if col_type in TYPE_MAP:
        return None
    if is_enum_type(col_type):
        enum_names = {e.name for e in schema.enums}
        if col_type in enum_names:
            return None
        if enum_names:
            return (
                f"Unknown enum type '{col_type}'. "
                f"Defined enums: {', '.join(sorted(enum_names))}"
            )
        return (
            f"Unknown enum type '{col_type}'. No enums are defined in the schema."
        )
    valid = ", ".join(sorted(TYPE_MAP.keys()))
    return f"Invalid column type '{col_type}'. Valid types: {valid}"


def _diff_markdown_text(staging: StagingManager) -> str:
    """Build a human-readable markdown changelog of pending changes."""
    from alter.diff_format import changes_to_markdown  # noqa: PLC0415

    if not staging.has_pending():
        return "_No pending changes._"
    return changes_to_markdown(staging.get_diff())


def _apply_to_code_impl(
    staging: StagingManager, project_root: Path, preview: bool = False
) -> str:
    """Write (or diff) committed schema to ORM model files."""
    from alter.generators.base import get_generator, _default_model_path  # noqa: PLC0415

    schema = staging.current_schema
    gen = get_generator(schema.orm)

    # Group tables by file_path
    file_groups: dict[str, list] = {}
    for t in schema.tables:
        fp = t.file_path or _default_model_path(schema, project_root)
        file_groups.setdefault(fp, []).append(t)

    diffs: list[str] = []
    writes: list[str] = []
    default_path = _default_model_path(schema, project_root)

    for rel_path, tables in file_groups.items():
        abs_path = project_root / rel_path
        # Build a per-file sub-schema for the generator (keep all enums for type resolution)
        file_schema = schema.model_copy(update={"tables": tables})
        # Only define enum classes that are owned by this file; others are imported elsewhere.
        # Enums with file_path=None route to the default model file only, not every file.
        local_enum_names = {
            e.name for e in schema.enums
            if e.file_path == rel_path or (e.file_path is None and rel_path == default_path)
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

    # Always scan the full project directory — same behaviour as `alter sync`.
    # The previous per-file approach only re-parsed files already tracked in
    # schema.alter, so new model files added after `alter init` were silently
    # ignored by both the canvas "Sync from Code" button and the MCP tool.
    result = parser.parse_directory(project_root)

    # Preserve canvas positions for tables that already existed.
    pos_map = {t.name: t.position for t in schema.tables}
    for t in result.schema.tables:
        if t.name in pos_map:
            t.position = pos_map[t.name]

    # Auto-layout any brand-new tables still at the default (0, 0).
    from alter.layout import auto_layout_tables  # noqa: PLC0415
    auto_layout_tables(result.schema.tables)

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
def add_table(
    name: str,
    file_path: str | None = None,
    columns: list[dict] | None = None,
) -> str:
    """Add a new table to the proposed schema.

    When ``columns`` is omitted a default ``id uuid PRIMARY KEY`` column is
    seeded automatically.  Use ``add_column`` to add further columns
    afterwards, or pass ``columns`` to define the full set up front.

    Args:
        name:      Table name (snake_case).
        file_path: Relative path to the ORM model file.  When omitted the
                   path is inferred from existing tables in the schema at
                   apply time — new tables land in the same directory as
                   the majority of existing tables.
        columns:   Optional list of column definitions.  Each element is a
                   dict with keys:

                     * ``name`` (required) — column name.
                     * ``type`` (required) — alter type: uuid, string, text,
                       int, bigint, float, decimal, bool, datetime, date,
                       time, json, bytes.
                     * ``primary_key`` (bool, default False)
                     * ``nullable``    (bool, default True, forced False when
                       primary_key is True)
                     * ``unique``      (bool, default False)
                     * ``default``     (str | None) — literal or keyword
                       (uuid4, now, utcnow, true, false, …)
                     * ``max_length``  (int | None)
                     * ``foreign_key`` (str | None) — ``"table.column"``
                     * ``index``       (bool, default False)

                   When omitted or empty, the single default id column is
                   seeded as described above.
    """
    staging = _get_staging()

    def apply(schema: AlterSchema) -> AlterSchema:
        s = copy.deepcopy(schema)
        if any(t.name == name for t in s.tables):
            raise ValueError(f"Table '{name}' already exists")
        tbl = Table(name=name, **{"file_path": file_path} if file_path else {})

        if not columns:
            # Default behaviour: seed a single uuid PK column.
            tbl.columns.append(
                Column(name="id", type="uuid", primary_key=True, nullable=False, default="uuid4")
            )
        else:
            new_relations: list[Relation] = []
            for col_spec in columns:
                col_name = col_spec.get("name")
                col_type = col_spec.get("type")
                if not col_name:
                    raise ValueError("Each column must have a 'name' field.")
                if not col_type:
                    raise ValueError(f"Column '{col_name}' must have a 'type' field.")

                # Validate column type.
                type_err = _validate_column_type(col_type, s)
                if type_err:
                    raise ValueError(type_err)

                col_primary_key = bool(col_spec.get("primary_key", False))
                # Respect explicit nullable; default True unless it's a PK.
                nullable_raw = col_spec.get("nullable")
                if col_primary_key:
                    col_nullable = False
                elif nullable_raw is None:
                    col_nullable = True
                else:
                    col_nullable = bool(nullable_raw)

                col_unique = bool(col_spec.get("unique", False))
                col_default = col_spec.get("default") or None
                col_max_length_raw = col_spec.get("max_length")
                col_max_length = int(col_max_length_raw) if col_max_length_raw is not None else None
                col_foreign_key = col_spec.get("foreign_key") or None
                col_index = bool(col_spec.get("index", False))

                # Validate FK target before touching the schema.
                relation: Relation | None = None
                if col_foreign_key:
                    parts = col_foreign_key.rsplit(".", 1)
                    if len(parts) != 2:
                        raise ValueError(
                            f"Invalid foreign_key format '{col_foreign_key}': "
                            "expected 'table.column'."
                        )
                    to_table_raw, to_column = parts
                    to_table = to_table_raw.rsplit(".", 1)[-1]  # strip optional schema prefix
                    target_tbl = next((t for t in s.tables if t.name == to_table), None)
                    if target_tbl is None:
                        raise ValueError(
                            f"FK target table '{to_table}' does not exist in schema."
                        )
                    if to_column not in {c.name for c in target_tbl.columns}:
                        raise ValueError(
                            f"FK target column '{to_table}.{to_column}' does not exist in schema."
                        )
                    relation = Relation(
                        name=f"{name}_{col_name}_fkey",
                        from_table=name,
                        from_column=col_name,
                        to_table=to_table,
                        to_column=to_column,
                        type="many-to-one",
                        on_delete="CASCADE",
                    )

                tbl.columns.append(
                    Column(
                        name=col_name,
                        type=col_type,
                        nullable=col_nullable,
                        unique=col_unique,
                        primary_key=col_primary_key,
                        default=col_default,
                        max_length=col_max_length,
                        foreign_key=col_foreign_key,
                    )
                )
                if col_index:
                    tbl.indexes.append(Index(columns=[col_name], unique=False))
                if relation is not None:
                    new_relations.append(relation)

            s.relations.extend(new_relations)

        s.tables.append(tbl)
        return s

    try:
        staging.propose(apply)
        if not columns:
            return f"Added table '{name}' with default id column."
        return f"Added table '{name}' with {len(columns)} column(s)."
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
    index: bool = False,
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
        index:       Create a non-unique index on this column (default False).
    """
    staging = _get_staging()

    def apply(schema: AlterSchema) -> AlterSchema:
        s = copy.deepcopy(schema)
        tbl = next((t for t in s.tables if t.name == table), None)
        if tbl is None:
            raise ValueError(f"Table '{table}' not found")
        if any(c.name == name for c in tbl.columns):
            raise ValueError(f"Column '{name}' already exists in '{table}'")

        # Validate column type before touching the schema.
        type_err = _validate_column_type(type, s)
        if type_err:
            raise ValueError(type_err)

        # Validate FK target BEFORE touching the schema so a bad FK never
        # leaves a partial column or a dangling relation behind.
        relation: Relation | None = None
        if foreign_key:
            parts = foreign_key.rsplit(".", 1)
            if len(parts) != 2:
                raise ValueError(
                    f"Invalid foreign_key format '{foreign_key}': expected 'table.column'."
                )
            to_table_raw, to_column = parts
            to_table = to_table_raw.rsplit(".", 1)[-1]  # strip optional schema prefix
            target_tbl = next((t for t in s.tables if t.name == to_table), None)
            if target_tbl is None:
                raise ValueError(
                    f"FK target table '{to_table}' does not exist in schema."
                )
            if to_column not in {c.name for c in target_tbl.columns}:
                raise ValueError(
                    f"FK target column '{to_table}.{to_column}' does not exist in schema."
                )
            relation = Relation(
                name=f"{table}_{name}_fkey",
                from_table=table,
                from_column=name,
                to_table=to_table,
                to_column=to_column,
                type="many-to-one",
                on_delete="CASCADE",
            )

        # Both column and relation are valid — append atomically.
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
        if relation is not None:
            s.relations.append(relation)
        if index:
            tbl.indexes.append(Index(columns=[name], unique=False))
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
    default: str | None = _UNSET,
    max_length: int | None = _UNSET,
    primary_key: bool | None = None,
    foreign_key: str | None = _UNSET,
    index: bool | None = None,
) -> str:
    """Modify properties of an existing column in the proposed schema.

    Only the provided fields are changed; omit a field to leave it unchanged.
    Pass ``default=None`` to remove an existing default value entirely.
    Pass ``max_length=None`` to remove an existing max_length value entirely.
    Pass ``foreign_key=None`` to remove an existing foreign key reference.
    Pass ``index=True`` to create a non-unique index; ``index=False`` to drop it.
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
            type_err = _validate_column_type(new_type, s)
            if type_err:
                raise ValueError(type_err)
            col.type = new_type
        if nullable is not None:
            col.nullable = nullable
        if unique is not None:
            col.unique = unique
        if default is not _UNSET:
            col.default = default
        if max_length is not _UNSET:
            col.max_length = max_length
        if primary_key is not None:
            col.primary_key = primary_key
            # A primary key column must be non-nullable.
            if primary_key:
                col.nullable = False
        if foreign_key is not _UNSET:
            # Validate and register the new FK, or clear the existing one.
            if foreign_key is not None:
                parts = foreign_key.rsplit(".", 1)
                if len(parts) != 2:
                    raise ValueError(
                        f"Invalid foreign_key format '{foreign_key}': "
                        "expected 'table.column'."
                    )
                to_table_raw, to_col_name = parts
                to_table = to_table_raw.rsplit(".", 1)[-1]
                target_tbl = next((t for t in s.tables if t.name == to_table), None)
                if target_tbl is None:
                    raise ValueError(
                        f"FK target table '{to_table}' does not exist in schema."
                    )
                if to_col_name not in {c.name for c in target_tbl.columns}:
                    raise ValueError(
                        f"FK target column '{to_table}.{to_col_name}' "
                        "does not exist in schema."
                    )
                # Remove any existing relation for this column before adding the new one.
                s.relations = [
                    r for r in s.relations
                    if not (r.from_table == table and r.from_column == column)
                ]
                s.relations.append(Relation(
                    name=f"{table}_{column}_fkey",
                    from_table=table,
                    from_column=column,
                    to_table=to_table,
                    to_column=to_col_name,
                    type="many-to-one",
                    on_delete="CASCADE",
                ))
            else:
                # foreign_key=None → remove the FK and its relation entry.
                s.relations = [
                    r for r in s.relations
                    if not (r.from_table == table and r.from_column == column)
                ]
            col.foreign_key = foreign_key
        if index is not None:
            col_name = new_name if new_name is not None else column
            if index:
                # Add a non-unique index if one doesn't already exist for this column.
                already = any(
                    idx.columns == [col_name] and not idx.unique
                    for idx in tbl.indexes
                )
                if not already:
                    tbl.indexes.append(Index(columns=[col_name], unique=False))
            else:
                # Remove any non-unique single-column index for this column.
                tbl.indexes = [
                    idx for idx in tbl.indexes
                    if not (idx.columns == [col_name] and not idx.unique)
                ]
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
            # Clear Column.foreign_key strings that pointed at the dropped table
            prefix = table + "."
            for t in s.tables:
                for col in t.columns:
                    if col.foreign_key and col.foreign_key.startswith(prefix):
                        col.foreign_key = None
        else:
            # Drop column
            tbl = next((t for t in s.tables if t.name == table), None)
            if tbl is None:
                raise ValueError(f"Table '{table}' not found")
            if not any(c.name == column for c in tbl.columns):
                raise ValueError(f"Column '{column}' not found in '{table}'")
            tbl.columns = [c for c in tbl.columns if c.name != column]
            # Remove relations that reference the dropped column
            s.relations = [
                r for r in s.relations
                if not (r.from_table == table and r.from_column == column)
                and not (r.to_table == table and r.to_column == column)
            ]
            # Clear Column.foreign_key strings that pointed at this column
            fk_ref = f"{table}.{column}"
            for t in s.tables:
                for col in t.columns:
                    if col.foreign_key == fk_ref:
                        col.foreign_key = None
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
            # Update relation objects
            for rel in s.relations:
                if rel.from_table == old_name:
                    rel.from_table = new_name
                if rel.to_table == old_name:
                    rel.to_table = new_name
            # Update Column.foreign_key strings ("old_table.col" → "new_table.col")
            prefix = old_name + "."
            for t in s.tables:
                for col in t.columns:
                    if col.foreign_key and col.foreign_key.startswith(prefix):
                        col.foreign_key = new_name + col.foreign_key[len(old_name):]
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
            # Update relation objects
            for rel in s.relations:
                if rel.from_table == table and rel.from_column == column:
                    rel.from_column = new_name
                if rel.to_table == table and rel.to_column == column:
                    rel.to_column = new_name
            # Update Column.foreign_key strings ("table.old_col" → "table.new_col")
            old_fk = f"{table}.{column}"
            new_fk = f"{table}.{new_name}"
            for t in s.tables:
                for c in t.columns:
                    if c.foreign_key == old_fk:
                        c.foreign_key = new_fk
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
    except Exception as exc:
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
    except Exception as exc:
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
        import_warnings: list[str] = []
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
            sql_result = import_sql(
                sql_text,
                orm=staging.current_schema.orm,
                file_path=staging.current_schema.metadata.sqlmodel_module,
            )
            imported = sql_result.schema
            import_warnings = sql_result.warnings

    except Exception as exc:
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

    existing_names = {t.name for t in staging.current_schema.tables}
    new_count = sum(1 for t in imported.tables if t.name not in existing_names)
    skipped_count = len(imported.tables) - new_count

    staging.propose(apply)

    skip_note = f" ({skipped_count} skipped — already in schema)" if skipped_count else ""
    msg = f"Imported {new_count} new tables{skip_note} from {format.upper()}."
    if import_warnings:
        warning_lines = "\n".join(f"⚠ {w}" for w in import_warnings)
        msg = f"{msg}\n\nWarnings:\n{warning_lines}"
    return msg


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
    except Exception as exc:
        return f"Error exporting schema: {exc}"


@mcp.tool()
def diff_markdown() -> str:
    """Return a human-readable markdown summary of pending changes.

    Suitable for copying into a PR description.
    """
    return _diff_markdown_text(_get_staging())


@mcp.tool()
def introspect_db(
    connection_string: str | None = None,
    schema: str = "public",
) -> str:
    """Import the schema from a live PostgreSQL database into the proposed schema.

    Tables already present are skipped.

    Args:
        connection_string: A libpq connection string or URL.  Defaults to the
            ``DATABASE_URL`` environment variable if not provided.
        schema: PostgreSQL schema name to introspect.  Defaults to
            ``"public"``.  Set this when your tables live in a custom
            schema (e.g. ``"myapp"``, ``"analytics"``).
    """
    cs = connection_string or os.environ.get("DATABASE_URL")
    if not cs:
        return (
            "Error: no connection string provided and DATABASE_URL is not set.\n"
            "Pass connection_string or set DATABASE_URL."
        )

    staging = _get_staging()

    try:
        from alter.importers.database import import_from_database  # noqa: PLC0415

        imported = import_from_database(
            cs,
            schema=schema,
            orm=staging.current_schema.orm,
        )
    except Exception as exc:
        return f"Error introspecting database: {exc}"

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
    from alter.generators.base import _default_model_path  # noqa: PLC0415
    staging = _get_staging()
    project_root = _get_path().parent
    parts: list[str] = []
    seen: set[str] = set()
    for tbl in staging.current_schema.tables:
        fp = tbl.file_path or _default_model_path(staging.current_schema, project_root)
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
