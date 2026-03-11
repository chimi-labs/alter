# Changelog

All notable changes to Alter are documented here.

## [0.1.3] — 2026-03-11

### Fixed

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
