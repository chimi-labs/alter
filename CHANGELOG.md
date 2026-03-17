# Changelog

All notable changes to Alter are documented here.

## [0.2.4] — 2026-03-17

### Bug Fixes

#### Duplicate FK constraints in migration SQL (Bug C)

When a new table with a foreign key column was added on the canvas, `_migration_sql()`
emitted the FK constraint twice: once inline inside the `CREATE TABLE` statement (via
`_table_to_sql()`), and a second time as a standalone `ALTER TABLE … ADD CONSTRAINT`
from the `add_relation` change handler.

Fixed by collecting the names of all tables being created in the current diff into an
`added_tables` set. The `add_relation` handler now skips emitting a separate FK
statement for any table in that set, since the `CREATE TABLE` block already contains
the inline reference.

#### `alter canvas` crashes on fresh project with no `schema.alter` file (Bug D)

Running `alter canvas` in a project that had never been initialised (no `schema.alter`
file yet) caused `watchfiles.watch()` to raise an error because the target path did not
exist. The traceback surfaced as an unhandled exception in the file-watcher thread,
producing a noisy and confusing error message in the terminal.

The file watcher thread now blocks on a `threading.Event` (`_file_created`) instead of
starting immediately. The event is signalled the first time `schema.alter` is written
(on the first Commit or Apply to Code), at which point `watchfiles.watch()` is called
on a path that is guaranteed to exist. Before the file exists, the canvas still loads
and works fully — changes are simply held in memory until the first commit.

#### `apply_to_code` silently using stale committed schema (Bug E)

When the canvas "Apply to Code" button was clicked with uncommitted staged changes
present, `_apply_to_code_impl` read `staging.current_schema` (the last committed
state) rather than `staging.proposed_schema` (the in-memory working state). Staged
changes were silently discarded from the code write while they remained visible on the
canvas, causing the model files to diverge from what the canvas showed.

Two-part fix:

1. **Canvas handler auto-commit** — `_handle_apply_to_code` in `canvas/server.py` now
   calls `staging.commit()` before delegating to `_apply_to_code_impl` whenever there
   are pending staged changes. This ensures the code write always reflects the current
   canvas state.

2. **MCP guard** — `apply_to_code()` in `mcp_server.py` now returns an early error
   message if `staging.has_pending()` is true, asking the caller to invoke
   `commit_changes()` first. This prevents the MCP path from silently applying a stale
   snapshot while an in-progress edit session is underway.

#### `alter import` creates spurious `app/models.py` on new projects (Bug F)

`alter import schema.sql` was routing imported tables to
`metadata.sqlmodel_module`, which defaults to `"app/models.py"`. On projects that had
no `app/` directory, this caused `alter apply` to create `app/models.py` as a new
file even though the project had no such layout.

The import command now calls `_default_model_path(current_schema, project_root)` to
infer the correct output file, applying the same priority logic used everywhere else
in the codebase: most-common directory across existing tracked tables → `app/` if it
exists → `models.py` in the project root. No phantom files are created.

#### MCP server emits `PydanticJsonSchemaWarning` on startup (Bug G)

The `_UNSET = object()` sentinel — used as the default for `default`, `max_length`,
and `foreign_key` parameters in `modify_column` so that callers can pass explicit
`None` to clear a field — caused Pydantic to emit a `PydanticJsonSchemaWarning` when
building the JSON schema for the MCP tool at server startup. The warning was harmless
(the sentinel works correctly at call time) but noisy.

Fixed by wrapping the tool-registration loop in `_LazyMCP._init_real` with a
`warnings.catch_warnings()` context manager that suppresses
`"Default value.*is not JSON serializable"` messages. The suppression is scoped
entirely to the registration calls; all other Pydantic warnings remain unaffected.

### Improvements

#### Column rename detection in migration SQL

Alter's diff engine is name-based and cannot distinguish a column rename from a
drop + add of the same type. When `_migration_sql()` detects this pattern — a
`drop_column` and an `add_column` on the same table whose dropped column and added
column share the same type — the generated SQL now includes a warning comment:

```sql
-- WARNING: 'orders.note' is being dropped while 'notes' (same type) is being added.
-- If this is a rename, replace the ADD+DROP below with:
--   ALTER TABLE orders RENAME COLUMN note TO notes;
```

This makes it easy to spot a likely rename and swap the destructive ADD+DROP for a
safe `RENAME COLUMN` before executing the migration.

#### Improved initial canvas layout

The ELK graph layout used for the first-open auto-arrange now produces cleaner ERD
diagrams. Changes:

- Direction changed from `RIGHT` to `DOWN` — tables flow top-to-bottom, which reads
  more naturally as an entity-relationship diagram.
- Node spacing increased from 60 px to 120 px, edge-to-node layer spacing from 80 px
  to 130 px — tables no longer overlap on medium-sized schemas.
- `BRANDES_KOEPF` node placement and `GREEDY` cycle-breaking strategies added for
  more compact, symmetrical layouts.
- Grid fallback spacing increased (`290 → 400 px` column width, `310 → 420 px` row
  height) for schemas that fall back to the simpler grid arrangement.

## [0.2.3] — 2026-03-15

### Bug Fixes

#### `alter mcp` crashes with a cryptic `ModuleNotFoundError` when `mcp < 1.2.0` (Bug A)

`alter mcp` calls `init_mcp()` which imports `FastMCP` from `mcp.server.fastmcp`. That
submodule was introduced in `mcp 1.2.0`; older installations raise a bare
`ModuleNotFoundError: No module named 'mcp.server.fastmcp'` with no indication of how
to fix it.

`init_mcp()` now wraps the import in a `try/except ImportError` and raises an
`AlterError` with an actionable message:

```
'alter mcp' requires mcp>=1.2.0, but mcp==1.1.3 is installed.
Upgrade with: pip install 'mcp>=1.2.0'
```

The error surfaces cleanly in the CLI (no "MCP server error:" prefix) because the CLI
already handles `AlterError` separately from generic exceptions.

A second guard wraps the cosmetic `_mcp_server.version` assignment in
`try/except AttributeError` so that future changes to `mcp` internals do not break
`alter mcp` startup.

#### `alter apply` spuriously rewrites `datetime.now(timezone.utc)` defaults on Python 3.11+ (Bug B)

When a model file contained a column with `default_factory=lambda: datetime.now(timezone.utc)`,
running `alter apply` on Python 3.11+ would rewrite the line even though nothing had
changed in the schema.

Root cause: `_parse_field_kwargs` normalises kwargs via `ast.unparse`, and the Python
`ast` module changed how it serialises zero-argument lambdas between versions —
Python ≤ 3.10 produces `"lambda :"` (with a space after `lambda`), while Python 3.11+
produces `"lambda:"` (no space). Because `ast.unparse` is applied to both the
existing code and the freshly generated schema line, the comparison reached different
sides of the `_DEFAULT_FACTORY_EQUIV` lookup depending on the Python version in use,
causing the surgical patcher to believe a change was needed when there was none.

Two-part fix in `generators/_surgical.py`:

1. **`_norm_lambda_ws()` helper** — strips extraneous whitespace between `lambda` and
   `:` for zero-argument lambdas (`re.sub(r"^lambda\s*:", "lambda:", s)`), making
   lambda strings compare equal across Python versions.

2. **Normalization applied consistently** — `_normalize_kw_for_eq` now calls
   `_norm_lambda_ws` on `default_factory` values after the `_DEFAULT_FACTORY_EQUIV`
   lookup, so both the existing-file side and the schema side are normalised before
   comparison. The rebuild path in `_rebuild_field_line` applies the same
   normalization when checking whether the existing value is canonically equivalent.

The `_DEFAULT_FACTORY_EQUIV` dict value for `utcnow` was also corrected from
`"lambda : datetime.now(timezone.utc)"` (with spurious space, matching old Python 3.10
`ast.unparse` output) to `"lambda: datetime.now(timezone.utc)"` (canonical form) — the
`_norm_lambda_ws` normalization then makes both forms match on all Python versions.

---

## [0.2.2] — 2026-03-15

### Bug Fixes

#### `alter apply` now removes deleted tables and columns from code (Bug 09)

`alter apply` (and `apply_to_code` via MCP) previously ignored deletions — removing a table or column from the canvas and running `alter apply` would report "No changes — files are already up to date." while the Python class or field remained in the model file untouched. Four layers were involved:

1. **Column deletion — update detection** (`generators/_surgical.py` · `_class_needs_update`): the function only checked that schema columns were present in the file, never the reverse. A `Field()`-style column that existed in the file but had been removed from the schema was silently skipped. Added a reverse check: if any `Field()` / `mapped_column()` column in the file is absent from the schema, the class is flagged for update. Bare annotations (`name: str`) are intentionally excluded from this check — they may be non-ORM helper attributes and should never be auto-deleted.

2. **Column deletion — patch logic** (`generators/_surgical.py` · `_surgical_patch_class`): when walking source lines, a `Field()`-style column not found in `schema_map` fell into the "keep verbatim" branch — indistinguishable from an unchanged field. Restructured the branch: `col_name in schema_map` → keep or rebuild as before; `col_name not in schema_map` → omit from output (deleted). `last_field_result_idx` is now only updated when output is actually produced, so new-column insertion remains correctly positioned after the last kept field.

3. **Table deletion** (`generators/sqlmodel.py`, `generators/sqlalchemy.py` · `update_models`): classes present in the file but absent from the schema were unconditionally skipped with a "leave untouched" comment. Now uses `tablename_to_class` (which maps `__tablename__` string → class name) to identify genuine ORM table classes. If a class has `__tablename__` and its table was deleted from the schema, it is removed from the file. Mixins, base classes, and helper classes without `__tablename__` are always left untouched.

4. **File discovery** (`mcp_server.py` · `_apply_to_code_impl`, `cli.py` · `apply`): `file_groups` was built only from `schema.tables`, so a file whose last table was deleted was never visited at all. Both the CLI `apply` command and `_apply_to_code_impl` now scan the project root for ORM-containing `.py` files (using the parser's `detect_orm()`) and add any file not already in `file_groups` with an empty table list, ensuring `update_models()` is called and can remove the deleted class. Virtual environments, `__pycache__`, and other non-source directories are skipped.

`alter apply --preview` correctly shows deleted fields and classes as red lines in the unified diff before any files are written.

---

#### MCP `undo`/`redo` always reported "Nothing to undo/redo" even after changes (Bug 01)

`undo()` and `redo()` in `StagingManager` return `None` both when the stack is empty and when the operation succeeds but reverts the schema to its original state. The MCP tool handlers checked the return value to distinguish the two cases, which was unreliable. Added explicit `can_undo()` and `can_redo()` methods to `StagingManager` and changed the tool handlers to check those before calling `undo()`/`redo()`.

#### Duplicate relations when tables are defined across multiple files (Bug 02)

`parse_directory()` in both the SQLModel and SQLAlchemy parsers accumulated `Relation` objects across all scanned files without deduplication. When a table (and its relations) appeared in more than one file — e.g. a base model re-exported from a package `__init__.py` — the schema ended up with duplicate relation entries, which produced broken Mermaid diagrams and duplicate `FOREIGN KEY` clauses in SQL export. Added `deduplicate_relations()` to `parsers/base.py` (mirroring the existing `deduplicate_tables()`), which keeps the first definition of each relation name and also prunes orphaned relations whose source table was itself dropped by deduplication. Both parsers now call it immediately after `deduplicate_tables()`.

#### MCP tool errors returned `isError: false` (Bug 05)

All `except` branches in `mcp_server.py` tool handlers returned error strings (`return f"Error: {exc}"`) instead of raising. FastMCP's internal error chain only sets `isError: true` on the MCP response when the tool function raises an exception; returning a string always produces `isError: false`, so AI assistants had no reliable way to detect failures. All 17 return-error patterns replaced with `raise` (re-raise in `except` blocks) or `raise ValueError(...)` (inline guards). The 27 affected tests in `test_mcp_server.py` updated to use `pytest.raises`.

#### `alter sync`/`alter diff` scan the wrong directory on multi-file projects (Bug 07)

`_find_model_dirs()` in `cli.py` selected the first existing directory from a candidate list (`app/models`, `app/`, `src/`, `cwd`) without checking whether the directory contained any Python files. An empty `app/` directory would win over the project root even if all models were in `cwd`. Two-part fix: (1) `_find_model_dirs()` now requires at least one `.py` file (recursive, skipping venv/cache dirs) via a new `_has_py_files()` helper — empty directories are excluded. (2) `sync` and `diff` commands now use the `file_path` values recorded in `schema.tables` as the primary source of truth when tables are already tracked, falling back to the directory heuristic only for empty schemas. `alter init` also now records the most common model file path in `schema.metadata.sqlmodel_module` rather than using a hardcoded `"app/models.py"` default.

---

## [0.2.1] — 2026-03-14

### Bug Fixes

#### CSRF: canvas server replaced wildcard CORS with canvas-specific origin
`Access-Control-Allow-Origin: *` allowed any website visited in the user's browser to make cross-origin requests to the canvas server's mutating endpoints (e.g. `/api/commit`, `/api/apply-to-code`). The server now reflects its own origin (`http://127.0.0.1:{port}`) in the ACAO header and adds `Vary: Origin`, so browser preflight checks block requests from other origins.

#### Mermaid exporter: `FK,UK` now both emitted for FK+unique columns
An `elif` on the unique check meant columns that are both a foreign key and unique (one-to-one relationship pattern) only showed `FK` in the diagram. Changed to `if` so both `FK` and `UK` attributes are emitted.

#### SQL importer: parenthesized DEFAULT expressions with spaces now captured correctly
`DEFAULT (1 + 2)` and `DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')` were truncated to just the first token because the `\S+` fallback stopped at the first space. Two-part fix: (1) `_DEFAULT_RE` now includes a parenthesized-expression branch `\((?:[^()]*|\([^()]*\))*\)` that handles one level of nesting; (2) the regex is now applied to the original `defn` string rather than the post-processed `rest`, because `_COL_DEF_RE`'s optional size group was silently stripping parentheses from DEFAULT expressions before `rest` was assembled.

---

## [0.2.0] — 2026-03-14

### New Features

#### `add_table` MCP tool now accepts a `columns` list

- `add_table` previously ignored any column definitions and always seeded a
  single `id uuid PRIMARY KEY` column.  The tool now accepts an optional
  `columns` parameter — a list of column-spec dicts supporting `name`, `type`,
  `primary_key`, `nullable`, `unique`, `default`, `max_length`, `foreign_key`,
  and `index` keys.
- When `columns` is omitted or empty the original default-id behaviour is
  preserved.  When provided, each column spec is fully validated (type,
  FK target existence) before any mutation occurs.
- FK columns automatically create a `Relation` object; `index: true` columns
  create a non-unique `Index`.
- Added 14 tests covering: all columns created, no spurious default-id,
  empty-list fallback, PK non-nullable, nullable defaults, explicit
  `nullable=False`, FK relation created, invalid FK error, invalid type error,
  missing name/type errors, index creation, return message count.

#### `add_column` MCP tool gains an `index` parameter

- `add_column` now accepts `index: bool = False`.  Passing `index=True` appends
  a non-unique `Index(columns=[name])` to the table alongside the new column.
- Type validation (`_validate_column_type`) is now applied in `add_column` so
  unknown types are rejected before the schema is touched.

#### `modify_column` MCP tool gains `primary_key`, `foreign_key`, and `index` parameters

- `primary_key: bool | None` — sets or clears the PK flag; also forces
  `nullable=False` when setting to `True`.
- `foreign_key: str | None` — validates the new FK target, removes the old
  `Relation` for this column, and appends a new one.  Pass `foreign_key=None`
  to remove an existing FK and its relation entirely.
- `index: bool | None` — `True` adds a non-unique index if one does not already
  exist; `False` drops the existing non-unique single-column index.
- Type changes in `modify_column` are now validated via `_validate_column_type`
  before being applied.
- `Pass foreign_key=None to remove an existing foreign key reference` is now
  documented in the tool docstring.

#### `introspect_db` MCP tool and `import_from_database` gain a `schema` parameter

- Both `introspect_db` (MCP) and `import_from_database()` previously queried
  only the `public` PostgreSQL schema — all six SQL queries had the schema name
  hardcoded as a string literal.
- Added `schema: str = "public"` to both.  All six queries now use a `%s`
  parameterised placeholder to avoid any SQL-injection risk and to support
  non-default schemas (e.g. `"myapp"`, `"analytics"`).
- Tables from a non-`public` schema have `schema_name` set on the resulting
  `Table` objects so generated SQL uses fully-qualified `schema.table`
  references.
- Added 26 tests: schema value flows into all 6 queries, no hardcoded
  `'public'` literals, public schema → no `schema_name` set, custom schema →
  `schema_name` set, `Table`/`Column`/PK/relation/position construction.

#### Canvas server now sets CORS headers

- The canvas HTTP server (`canvas/server.py`) responded without any
  `Access-Control-*` headers, blocking cross-origin access from browser
  extensions and locally-served UIs.
- Added `_send_cors_headers()` helper and `do_OPTIONS()` preflight handler to
  `_Handler`.  CORS headers are now appended in both `_send()` (regular
  responses) and `_serve_events()` (SSE stream).
- 16 tests using a real `CanvasServer` on an OS-assigned port, covering GET,
  POST, 404, and OPTIONS preflight on both mapped and unmapped paths.

### Fixed

#### `alter apply` makes unnecessary changes to working code (Bug 17)

Three independent causes of spurious diffs when running `alter apply` on
already-correct model files:

1. **`uuid4` rewritten to `uuid.uuid4`** — `_DEFAULT_FACTORY_EQUIV` lacked an
   entry for `uuid4` (the direct-import form).  Added `"uuid4": "uuid.uuid4"`
   so the two forms are recognised as equivalent and the existing hand-written
   form is preserved verbatim.

2. **`import uuid` injected when not needed** — the import-insertion pass
   (`_insert_missing_imports`) already filters out imports for names not
   referenced in the new output; this was already correct once fix 1 stopped
   the `uuid4→uuid.uuid4` rewrite.

3. **Double-quoted strings rewritten to single-quoted** — `ast.unparse()`
   normalises all strings to single quotes, so unchanged `foreign_key="user.id"`
   kwargs were being rewritten.  Added `_parse_field_kwargs_raw_text()`, which
   uses AST column-offset information to extract verbatim kwarg text, and a new
   branch in `_rebuild_field_line()` that re-emits the raw text for unchanged
   string kwargs.

22 tests covering: `_normalize_kw_for_eq` uuid4 equivalence,
`_field_kwargs_equal` uuid4↔uuid.uuid4, `_parse_field_kwargs_raw_text`,
`_rebuild_field_line` uuid4/quote preservation, `surgical_update_class` no-op
for uuid4, `update_models` no spurious `import uuid`/`timezone` injection,
double-quoted FK preservation in full round-trip.

### Cleanup

#### Unused imports and dead code removed

- `generators/sqlmodel.py`: removed `import keyword`, `from pathlib import
  Path`, `_default_model_path`, `_imported_names`, `is_enum_type`.
- `generators/sqlalchemy.py`: removed `from pathlib import Path`,
  `_default_model_path`, `alter_to_sql`, `is_enum_type`.
- `mcp_server.py`: removed `diff_schemas`, `EnumDef` unused imports; removed
  dead `col_ref` variable in `modify_column`.
- `canvas/server.py`: removed `SchemaChange`, `diff_schemas` unused imports;
  removed dead `Relation as Rel` local import, `cur_tables`/`cur` dead
  variables in `_migration_sql`.
- `importers/database.py`: removed `from pathlib import Path`, `Position`
  unused import.
- `importers/sql.py`: removed `Punctuation` unused import.
- `cli.py`: removed `import os` unused import; removed dead `_match_file_paths()`
  function (defined but never called).

#### Redundant exception tuples collapsed

- `except (AlterError, Exception)` is logically equivalent to `except Exception`
  since `AlterError` is a subclass of `Exception`.  All six occurrences across
  `cli.py` and `mcp_server.py` replaced with `except Exception`.
- Also collapsed the pre-existing `except (ImportError, RuntimeError, Exception)`
  in `introspect_db` for the same reason.

## [0.1.9] — 2026-03-12

### Fixed

#### `validate_schema` accepts invalid SQL/Python identifiers as table/column names

- `validate_schema()` in `validate.py` only checked for empty and duplicate
  names.  Names like `123users`, `user-name`, or `select` passed silently even
  though they generate broken DDL (`CREATE TABLE 123users` is a SQL syntax error)
  or break Python codegen (hyphens are not valid Python identifiers).
- Added `_VALID_IDENTIFIER_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')` and
  `_SQL_RESERVED` frozenset (34 common SQL keywords) to `validate.py`.
- Structurally invalid names (digit-start, hyphens, spaces, etc.) now produce
  a **`"error"`**-severity `ValidationIssue`; these will always break SQL export.
- SQL reserved words that are otherwise valid identifiers produce a
  **`"warning"`**-severity issue; some databases handle them via quoting but
  they're risky enough to flag.  The check is case-insensitive so `SELECT`,
  `Select`, and `select` all trigger.
- The same checks apply to both table names and column names.
- Added 22 tests across `TestTableNameIdentifiers` and `TestColumnNameIdentifiers`
  covering valid names (plain, underscore-prefix, mixed-case), structurally
  invalid names (digit-start, hyphen, space, dot, `@`), and reserved words.

#### `add_column` MCP tool creates dangling Relation for nonexistent FK targets

- `add_column` in `mcp_server.py` appended the column first and then created a
  `Relation` object referencing whatever `foreign_key` string was supplied,
  without checking that the target table or column actually exists.  A call like
  `add_column(table="users", name="org_id", foreign_key="ghost.id")` would leave
  a broken `Relation` in the schema that `alter validate` flags as an error and
  SQL export turns into an invalid `REFERENCES` clause.
- Added validation of `to_table` and `to_column` existence **before** the
  column or relation is appended, so a failed FK check leaves the schema
  completely unchanged (no partial column, no dangling relation).
- Also strips invalid `foreign_key` values silently in the canvas
  `add_column` handler (`canvas/server.py`) for defence-in-depth.
- Added 5 tests: nonexistent table → error, nonexistent column → error,
  malformed format → error, no partial column left on failure, valid FK → both
  column and relation created.

#### Canvas `modify_column` accepts arbitrary fields without validation

- `_handle_propose` in `canvas/server.py` applied any key sent in the
  `modify_column` payload directly via `setattr` as long as `hasattr()` was
  truthy.  This allowed clients to mutate internal fields (e.g. `primary_key`,
  `id`) and to set arbitrary column types including invalid strings.
- Introduced `_MODIFIABLE_COL_FIELDS` whitelist (`name`, `type`, `nullable`,
  `unique`, `default`, `max_length`, `index`, `foreign_key`) — only updates
  matching a whitelisted key are applied.
- `type` values are validated against `TYPE_MAP` and the schema's declared
  enums; unknown types are silently rejected.
- `name` updates are handled as a proper rename: duplicate/empty names are
  rejected, relation objects and `Column.foreign_key` strings are updated to
  reflect the new name (mirroring `rename_entity` in `mcp_server.py`).
- Extracted the logic into a module-level `_apply_modify_column()` helper so
  the behavior can be unit-tested without spinning up an HTTP server.
- Added 16 tests covering the whitelist, type validation, rename cascading, and
  edge cases.

## [0.1.8] — 2026-03-12

### Fixed

#### SQL DDL export omits `CREATE INDEX` for columns with `index=True`

- `export_sql()` in `exporters/sql.py` only emitted `CREATE TABLE` blocks.
  It now appends `CREATE INDEX idx_{table}_{col} ON {qualified} ({col});` after
  each table for every column where `index=True` and `primary_key=False`.
  Schema-qualified table names (`schema.table`) are used when `schema_name` is
  set, matching the existing `CREATE TABLE` behaviour.

#### Markdown diff output silently drops index and enum change types

- `changes_to_markdown()` in `diff_format.py` only handled 7 of the 12 change
  types defined by the diff engine. The remaining 5 (`add_index`, `drop_index`,
  `add_enum`, `drop_enum`, `modify_enum`) were silently dropped, so
  `alter diff --format markdown` never showed index or enum changes.
  Added the missing sections and `elif` branches for all five types.

#### `alter import` always reports parsed count, not actual new-table count

- Both `alter import` (CLI) and the `import_schema` MCP tool always printed
  `"Imported N tables"` using the number of tables parsed from the source file,
  even when all of them were already present and skipped. Now computes
  `new_count` and `skipped_count` by diffing parsed names against
  `staging.current_schema` before proposing, and reports both:
  `"Imported 0 new tables (1 skipped — already in schema)"`.

#### `alter init` silently overwrites existing `schema.alter`

- Running `alter init` a second time would destroy canvas positions and manual
  edits without any warning. Added an existence check: if the target file
  already exists and `--force` is not set, the command prints the existing table
  count and prompts `"Overwrite? [y/N]"` via `click.confirm()`. If the user
  declines, the command aborts without touching the file. The new `--force` flag
  skips the prompt for scripted or CI use.

#### `_find_alter_file` picks alphabetically-first `.alter` file instead of `schema.alter`

- When multiple `.alter` files existed in a directory, `_find_alter_file()`
  returned `sorted()[0]`, which could silently pick `custom.alter` over
  `schema.alter`. Now checks for a file literally named `schema.alter` first
  and returns it immediately if found. When multiple files exist and none is
  `schema.alter`, a warning is printed to stderr listing the candidates and
  suggesting `--file` to disambiguate.

#### Canvas auto-positioning can place new tables over manually-dragged ones

- `_auto_position_new()` started the grid index from `len(positioned)`, which
  assumed all existing tables fill the grid sequentially. A table dragged to
  grid slot N would be overlapped by the next auto-placed table.
  Fixed by building an `occupied` set of existing positions and skipping any
  candidate grid slot whose coordinates fall within `_TABLE_W × _TABLE_H`
  (250 × 280 px) of an already-occupied position. Each newly placed table is
  added to `occupied` so subsequent tables in the same pass also avoid it.

## [0.1.7] — 2026-03-12

### Fixed

#### Canvas: enum values displayed as `[object Object]`

- Added `enumValueDisplay()` helper in `canvas.js` that renders both plain
  strings and `EnumMember` objects (`{member_name, value}`) correctly.
  When the member name and value differ, displays as `"MEMBER = value"`.
- Added `parseEnumValue()` to parse textarea lines back into structured
  `{member_name, value}` objects on save, preventing data loss.

#### Canvas: enum add / edit / delete were silent no-ops

- The `add_enum`, `edit_enum`, and `drop_enum` operations sent by the canvas
  were never handled in `_handle_propose` on the server — they silently did
  nothing. Added `add_enum` handler; edit and delete are intentionally excluded
  (see below).

#### Canvas: restrict enum mutations to add + read only

- Removed Edit and Delete buttons from the enum list. Renaming or deleting an
  enum must be done in code directly to avoid cascading edge cases. The canvas
  only supports adding new enums and reading existing ones.

#### `_migration_sql` silently skips `add_relation` / `drop_relation`

- The handler was looking for `ch.details["relation"]` (a key that never
  exists); the diff engine actually provides `ch.table`, `ch.column`, and
  `ch.details["to"]` as `"to_table.to_column"`. Rewrote both branches to
  read the correct fields. `add_relation` now also looks up `on_delete` from
  the proposed schema's relations list.

#### `_migration_sql` ignores `nullable`, `unique`, and `default` changes

- The `modify_column` branch only emitted a `TYPE` change (unconditionally).
  It now emits each applicable statement independently:
  `SET NOT NULL` / `DROP NOT NULL`, `ADD CONSTRAINT … UNIQUE` /
  `DROP CONSTRAINT IF EXISTS`, `SET DEFAULT …` / `DROP DEFAULT`, and
  `TYPE … USING col::TYPE` (only when the type actually changed).
  Reuses `_format_default()` from the SQL exporter for correct quoting.

#### `rename_entity` leaves stale `Column.foreign_key` strings

- After renaming a table, `Column.foreign_key` strings in other tables
  (e.g. `"users.id"`) were not updated. Added a sweep of all columns after
  updating `Relation` objects.
- Same fix for column renames: `"table.old_col"` → `"table.new_col"` across
  all columns in all tables.

#### `modify_column` cannot clear `default` or `max_length`

- Used `None` check (`if default is not None`) which made it impossible to
  clear a column's default by passing `default=None`. Introduced a
  module-level `_UNSET = object()` sentinel; both `default` and `max_length`
  now default to `_UNSET` so `None` is correctly treated as "clear this field".

#### `validate_schema` misses duplicate table names

- A schema with two tables sharing the same name passed validation with no
  errors. Added a pre-pass that reports `severity="error"` for each duplicate
  occurrence, preventing broken code generation and SQL export.

### Added

#### `alter.__version__`

- `alter/__init__.py` now exposes `__version__` via `importlib.metadata`,
  making `import alter; alter.__version__` work at runtime.
  `alterdb.__version__` also works via the existing compatibility shim.

## [0.1.6] — 2026-03-12

### Fixed

#### Generators emit deprecated `datetime.utcnow` (Python 3.12+)

- Both the SQLModel and SQLAlchemy generators now emit
  `lambda: datetime.now(timezone.utc)` instead of the bare
  `datetime.utcnow` reference, which was deprecated in Python 3.12 and
  will raise a `DeprecationWarning` at runtime.  The `_build_imports`
  helper in each generator now also adds `timezone` to the
  `from datetime import …` line whenever a `utcnow` default is present,
  so the generated file is always importable without manual edits.

### Added

#### `AlterSchema(strict=False)` — opt-out from constructor type validation

- `AlterSchema` now accepts a `strict: bool = True` keyword argument.
  When `strict=False` the `validate_enum_references` model validator is
  skipped, so schemas that reference types not yet in the type registry
  (e.g. during incremental parsing or test fixtures) can be constructed
  without raising `ValueError`.  The field is excluded from JSON
  serialisation so existing `.alter` files are unaffected.

### Internal

- Extracted `changes_to_markdown()` into a new `alter/diff_format.py`
  module, removing an unnecessary import coupling between the CLI and the
  MCP server.
- Moved shared AST helpers (`_FileResult`, `_is_enum_class`,
  `_parse_enum_class`, `_get_table_schema`, `_node_to_name`,
  `_node_to_type_str`, `_const_bool`, `_make_relation`) and three
  concrete `BaseParser` methods (`_search_roots`, `_collect_import_deps`,
  `_phase1_collect_enums`) into `parsers/base.py`, eliminating the
  duplication between the SQLModel and SQLAlchemy parsers.
- Moved shared generator helpers (`_class_name`, `_safe_member_name`,
  `generate_enum_class`, `_imported_names`, `_collect_stdlib_imports`)
  and three concrete `BaseGenerator` methods (`_collect_missing_imports`,
  `_insert_missing_imports`, `preview_apply`) into `generators/base.py`.
  Each ORM backend now only implements `_build_imports` with its own
  specific import lines.
- Removed the empty `alter/file_watcher.py` stub (file-watching logic
  lives in `canvas/server.py`).

## [0.1.5] — 2026-03-11

### Fixed

#### `import alterdb` shim package

- Added `src/alterdb/__init__.py` re-exporting `alter`, and `src/alterdb` to
  `pyproject.toml` packages, so `import alterdb` works as a drop-in alias for
  `import alter`.

#### `schema_name` not extracted when `__table_args__` is a tuple

- `_get_table_schema` in both parsers now handles the common
  `(__table_args__ = ({"schema": "x"}, constraint, …))` tuple form in addition
  to the bare `{"schema": "x"}` dict form.

#### SQL and Mermaid exporters ignore `schema_name`

- SQL exporter: added `_qualified_name()` helper; `CREATE TABLE` headers and
  `REFERENCES` clauses now emit `schema.table` when `schema_name` is set.
  `_table_to_sql`'s `table_by_name` parameter is optional for back-compat.
- Mermaid exporter: entity names and relation lines use `schema_table`
  (underscore-joined) for valid Mermaid identifiers.

#### `alter validate` rejects schema-prefixed foreign keys

- Added `_parse_fk_reference()` that accepts both `"table.column"` and
  `"schema.table.column"`; the error message now documents both formats.

#### `Optional[List[Any]]` with `sa_column=Column(JSON)` silently dropped

- `_is_primitive_element()` distinguishes primitive element types from model
  classes; `_annotation_is_list` no longer skips `list[primitive]`; and
  `_resolve_annotation` returns `"json_array"` for `List[primitive]` (including
  `List[Any]`).

#### Unreferenced enums from non-SQLModel-table files collected into `schema.alter`

- `parse_directory` post-filters `schema.enums` to only retain enums actually
  referenced by a column type in at least one parsed table.

#### `alter apply` rewrites `Field()` calls unnecessarily

Three sub-fixes in `generators/_surgical.py`:

- **Spurious default rewrite** — `_normalize_kw_for_eq()` treats `default={}`
  as equivalent to `default_factory=dict` (and `default=[]` ≡
  `default_factory=list`), so `_field_kwargs_equal` returns `True` and no
  rebuild is triggered when nothing truly changed.
- **Kwarg order shuffled** — the merging loop in `_rebuild_field_line` now
  detects the mutable-default equivalence and preserves the existing kwarg name
  and position rather than dropping it and appending `default_factory` at the
  end.
- **Trailing inline comments stripped** — `_extract_trailing_comment()` captures
  any `# …` suffix after the closing `)` and re-attaches it to the rebuilt line.

## [0.1.4] — 2026-03-11

### Fixed

#### Unreferenced enums from DTO / Pydantic / utility files collected into `schema.alter`

- **`alter init` swept up every `Enum` subclass it found**, including enums from
  DTO files, Pydantic-only models, and utility scripts that share a directory
  with the real SQLModel models. These spurious enums cluttered `schema.alter`
  and appeared in Mermaid and SQL exports.

  Fix: `parse_directory` now post-filters `schema.enums` after all phases
  complete. Only enums whose name matches at least one `col.type` across all
  parsed SQLModel table columns are kept. Enums that are defined in the scanned
  tree but never referenced by any column are silently discarded.

#### `Optional[List[Any]]` columns silently dropped by SQLModel parser

- **Column annotated as `Optional[List[Any]]` was absent from `schema.alter`**
  with no warning — the parser treated every `List[X]` / `list[X]` subscript as
  a relationship back-reference and skipped it, regardless of the element type.

  Fix: added `_is_primitive_element()` which returns `True` for builtin and
  `typing` primitive names (`Any`, `str`, `int`, `dict`, `Dict[K,V]`, etc.).

  - `_resolve_annotation` now returns `"json_array"` for `List[primitive]` and
    `"_relationship"` only when the element type is a model class or forward-ref
    string. Also handles `Dict[K, V]` → `"json"`.
  - `_annotation_is_list` (early-exit guard) now passes `list[primitive]`
    annotations through to `_resolve_annotation` instead of silently dropping
    them — so bare `list[Any]` / `list[str]` etc. are no longer lost even
    without an `Optional` wrapper.
  - `_extract_base_class_columns` now emits a `warnings.warn` instead of
    silently skipping columns with truly unresolvable type annotations.

#### `alter validate` rejected schema-qualified foreign keys as format errors

- **`alter validate` exited with code 1 for every schema-qualified FK** — the
  validator checked the raw `foreign_key` string against a strict two-part
  `table.column` regex, so columns declared as
  `Field(foreign_key="myschema.orders.id")` produced spurious errors like:
  > Foreign key 'myschema.orders.id' must be in 'table.column' format

  The parser handled these FKs correctly (resolves relations, builds DDL), so
  `alter export` and `alter diff` worked fine — only `alter validate` was broken.

  Fix: added `_parse_fk_reference(fk)` which returns `(schema, table, column)`
  for both `"table.column"` and `"schema.table.column"` forms. The validator now
  accepts both, resolves the referenced table by bare name (stripping the schema
  prefix), and updates the format-error message to describe both valid forms.

#### SQL exporter ignored `schema_name` — `CREATE TABLE` omitted schema prefix

- **`CREATE TABLE orders` instead of `CREATE TABLE myschema.orders`** — the SQL
  DDL exporter built table name strings from `table.name` only and never read
  `table.schema_name`, so all exported DDL was unqualified even when the `.alter`
  file had the correct schema set. `FOREIGN KEY … REFERENCES` clauses were
  similarly unqualified.

  Fix: added a `_qualified_name(table)` helper in `exporters/sql.py` that returns
  `schema.table` when `schema_name` is set. Applied to the `CREATE TABLE` header
  and every `REFERENCES` target in `FOREIGN KEY` constraints (resolved via a
  `table_by_name` lookup so cross-schema references are always correct).

  Also fixed `exporters/mermaid.py`: tables with a `schema_name` now use a
  `schema_table` identifier (underscore-joined, valid Mermaid syntax) so that
  multi-schema diagrams are unambiguous. Both entity blocks and relation lines
  use the qualified name consistently. Tables without a schema are unaffected.

#### `schema_name` not extracted when `__table_args__` is a tuple

- **Tuple form of `__table_args__` lost the schema name** — SQLAlchemy/SQLModel
  requires the tuple form when combining `Index` or `UniqueConstraint` objects
  with table-level keyword options:
  ```python
  __table_args__ = (Index("ix_foo", "col"), {"schema": "myschema"})
  ```
  The parser only handled the plain-dict form, so any model using the tuple form
  got `schema_name=None` and lost its PostgreSQL schema on the next `alter apply`.

  Fix: `_get_table_schema` in both the SQLModel and SQLAlchemy parsers now
  handles `ast.Tuple` nodes by scanning elements in reverse and using the first
  `ast.Dict` found as the options dict (matching SQLAlchemy's own convention that
  the last tuple element must be the keyword-options dict).

#### `sa_column=Column(JSON)` type ignored by parser (Fix 10)

- **`Optional[str]` with `sa_column=Column(JSON)` stored as type `"string"`** — the SQLModel
  parser resolved the alter type purely from the Python annotation and ignored the SQLAlchemy
  column expression. Columns annotated as `str` but backed by `JSON` or `JSONB` were stored
  with the wrong type, causing the canvas to show them as strings and `alter apply` to regenerate
  them without the JSON column type.

  Fix: `_parse_field_call` now inspects the `sa_column` / `sa_type` expression stored in
  `extra_kwargs` after all kwargs are collected and promotes the alter type to `"json"` when the
  expression contains `JSON` or `JSONB`, or to the enum class name when it matches
  `SQLEnum(EnumClass, ...)` and that class is a known enum.

#### `__table_args__` schema not preserved on full regeneration (Fix 7)

- **PostgreSQL schema lost on `alter apply`** — when `alter apply` wrote a model file from scratch
  (or appended a new class), `__table_args__ = {"schema": "myschema"}` was never emitted because
  the schema value was not stored in the `.alter` file. Only the surgical patcher happened to
  preserve it as a non-field line.

  Fix: added `schema_name: Optional[str]` to the `Table` schema model. Both the SQLModel and
  SQLAlchemy parsers now extract the value from `__table_args__` via a new `_get_table_schema()`
  AST helper. The SQLModel generator emits `__table_args__ = {"schema": "..."}` whenever
  `schema_name` is set.

#### Enum duplication on `alter apply` (Fix 6)

- **Every `alter apply` run added a duplicate copy of every enum** — the apply loop checked
  whether a model class was already present but did not do the same for enum classes. On the
  second run, each `class RoleEnum(str, Enum)` block appeared twice in the output file.

  Fix: `update_models` and `generate_models` now collect `local_enum_names` from the existing
  file content and skip emitting any enum whose name is already present.

#### SQL DDL export emits invalid default literals (Fix 3)

- **`ALTER TABLE … SET DEFAULT '[]'`** (and similar) — the SQL DDL exporter's
  `_format_default` helper emitted Python-style literals (`[]`, `{}`, `True`, `False`,
  `datetime(…)`) verbatim into SQL `DEFAULT` clauses, producing invalid DDL that most
  databases reject.

  Fix: `_format_default` now maps Python literals to their SQL equivalents: `[]` → `'[]'`,
  `{}` → `'{}'`, `True` / `False` → `TRUE` / `FALSE`, `datetime(…)` → quoted ISO string.
  Numeric literals are emitted unquoted; everything else is single-quoted and escaped.

#### `alter canvas` crash on projects with `mcp < 1.2.0`

- **`ModuleNotFoundError: No module named 'mcp.server.fastmcp'`** — `alter canvas`
  crashed in projects where an older `mcp` version was installed as a dependency
  (e.g. pinned transitively by uvicorn/starlette). Root cause: `FastMCP` was imported
  at module level in `mcp_server.py`, so any import of that module — including the
  canvas server's import of two helper functions — triggered the crash.

  Fix: introduced a `_LazyMCP` proxy that buffers `@mcp.tool()` / `@mcp.resource()`
  decorator calls at import time without touching FastMCP. The real `FastMCP` instance
  is created inside `init_mcp()`, which is only called when `alter mcp` is explicitly
  invoked. The `mcp` dependency floor was also reverted from `>=1.2.0` back to `>=1.0`
  so that `uv add alterdb` does not conflict with projects pinned to older versions.

#### `alter apply` minimal-diff principle — five additional bugs

- **Schema-qualified foreign keys stripped** — `foreign_key="myschema.table.column"` was
  written back as `foreign_key="table.column"`, breaking SQLAlchemy's cross-schema FK
  resolution. Both the SQLModel and SQLAlchemy parsers now store `Column.foreign_key`
  verbatim. `Relation.to_table` still holds the unqualified table name for the canvas.

- **`Optional[list]` rewritten as `Optional[dict]`** — bare `list` / `List` annotations
  were parsed as the `json` alter type, which maps back to Python `dict`. A new dedicated
  `json_array` alter type (`TypeEntry("list", "JSONB")`) ensures `list` round-trips as
  `list`.

- **`Optional[str]` PK annotation forced to `str`** — the surgical updater now treats
  `Optional[X]` as semantically equivalent to `X` on primary-key fields, so an existing
  `id: Optional[str] = Field(primary_key=True)` is left untouched.

- **Multi-line `Field()` calls collapsed to a single line** — when a field that needed
  updating was originally formatted across multiple lines, the replacement was always
  emitted as a single line. The surgical patcher now preserves the original multi-line
  style.

- **`Field()` kwarg order changed on replacement** — when a field did need updating, the
  generator's canonical kwarg order replaced the hand-written one. The surgical patcher
  now rebuilds only the kwargs that actually changed, keeping everything else in its
  original position.

#### `parse_directory` Phase 2 exhausted generator (Fix 11)

- **Second pass over `iter_py_files` yielded nothing** — `parse_directory` iterates the
  file list twice: once to collect enum definitions (Phase 1) and once to parse model classes
  (Phase 2). If `iter_py_files` ever returned a generator instead of a list, Phase 2 would
  silently see zero files and produce an empty schema.

  Fix: both the SQLModel and SQLAlchemy `parse_directory` implementations now wrap the call
  in `list()` to materialise the file list before the first pass. The `iter_py_files` docstring
  now explicitly documents this as a contract.

#### Parser & round-trip fidelity (7 bugs fixed in earlier commit)

- **Schema-qualified foreign keys** (`"schema.table.column"`) now parse correctly in
  both the SQLModel and SQLAlchemy parsers. Previously the schema name was used as the
  table name, breaking canvas relation lines.

- **Lambda `default_factory`** is no longer silently dropped. Expressions like
  `default_factory=lambda: str(uuid.uuid4())` are preserved verbatim and re-emitted
  by the generator on `alter apply`.

- **`sa_column` and `sa_type` kwargs** are no longer discarded. They are now stored in
  a new `Column.extra_kwargs` passthrough dict and re-emitted verbatim, so JSON columns
  (`sa_column=Column(JSON)`) and schema-qualified enums survive a round-trip.

- **Validator kwargs** (`regex`, `ge`, `le`, `gt`, `lt`, `min_length`) are no longer
  in the `pass` block. They are also captured in `Column.extra_kwargs` and re-emitted,
  so `Field(regex=r"^[a-z_]+$")` is preserved on apply.

- **Dict and list literal defaults** (`default={}`, `default=[]`) are no longer dropped.
  They are stored as `"{}"` / `"[]"` and emitted as `default_factory=dict` /
  `default_factory=list` to avoid the mutable-default antipattern.

- **`datetime.now` vs `datetime.utcnow`** are now kept distinct. Previously both were
  emitted as `datetime.utcnow`; `datetime.now` (local time) now round-trips correctly.

- **Enum member names are preserved.** `ENDUSER = "enduser"` previously stored only the
  value `"enduser"`, causing the surgical updater to insert duplicate members. The schema
  now stores `(member_name, value)` pairs via a new `EnumMember` model. Existing
  `.alter` files with plain-string values are automatically migrated on load.

### Added

- **`import alterdb` compatibility shim** — `pip install alterdb` now makes both
  `import alterdb` and `import alter` work. A thin `alterdb/__init__.py` re-exports
  everything from the `alter` package via `from alter import *`, following the same
  pattern as `Pillow`/`PIL` and `scikit-learn`/`sklearn`. Existing code using
  `import alter` is unaffected.

- **`alter --version`** — the CLI now accepts a `--version` flag that prints the installed
  package version (e.g. `alterdb, version 0.1.4`) and exits. Implemented via
  `@click.version_option(package_name="alterdb")`.

- `Table.schema_name: Optional[str]` — stores the PostgreSQL schema extracted from
  `__table_args__ = {"schema": "..."}`. Round-trips through `.alter` files and is re-emitted
  by the SQLModel generator on `alter apply`.

- `Column.extra_kwargs: Optional[dict[str, str]]` — passthrough dict for Field() kwargs
  that have no dedicated schema field. Any kwarg stored here is re-emitted verbatim by
  the generator.

- `EnumMember` schema model with `member_name` (Python identifier) and `value` (string
  literal) fields. `EnumDef.values` now holds a list of `EnumMember` objects.
  Backward-compatible: existing `.alter` files with `values: ["a", "b"]` are accepted
  and auto-upgraded.

- New `json_array` alter type for bare `list` / `List` annotations. Columns typed as
  `Optional[list]` now round-trip correctly instead of becoming `Optional[dict]`.

### Known behaviour

- **`Field()` kwarg order normalised on first generation** — when `alter apply`
  writes a model file for the first time (or appends a brand-new class to an
  existing file), the generator emits `Field()` kwargs in a canonical order:
  `primary_key`, `default`/`default_factory`, `foreign_key`, `unique`, `index`,
  `max_length`, then any passthrough kwargs.  This is intentional: a freshly
  generated file is consistent and readable regardless of how the kwargs were
  ordered in an earlier hand-written version.

  Subsequent runs of `alter apply` that only modify individual fields use the
  *surgical patcher* (`_rebuild_field_line`), which always preserves the
  existing kwarg order — so repeated applies produce no spurious diffs.

- **Mutable defaults corrected to `default_factory`** — `alter apply` rewrites
  `default={}` as `default_factory=dict` and `default=[]` as
  `default_factory=list`. This is intentional: mutable default arguments are a
  well-known Python antipattern where the same object is shared across all
  instances, causing subtle state-leak bugs. The corrected form is always safe
  and idiomatic. There is no option to preserve the original style, as doing so
  would mean round-tripping a known bug.

### Documentation

- README: added `uv tool install alterdb` as a recommended workaround when
  `alterdb` has dependency conflicts with packages in the host project.
