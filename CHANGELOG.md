# Changelog

All notable changes to Alter are documented here.

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
