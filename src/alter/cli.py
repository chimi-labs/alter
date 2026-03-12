"""Alter CLI — AI-assisted schema management with a visual canvas."""

from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

import click

from alter.errors import AlterError


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _find_alter_file(cwd: Path) -> Path | None:
    """Search *cwd* and two parent levels for a *.alter file."""
    for directory in [cwd, cwd.parent, cwd.parent.parent]:
        matches = sorted(directory.glob("*.alter"))
        if matches:
            return matches[0]
    return None


def _require_alter_file(alter_file: str | None, cwd: Path | None = None) -> Path:
    """Resolve the .alter file path or exit with a helpful message."""
    if alter_file:
        p = Path(alter_file)
        if not p.exists():
            raise click.ClickException(f"File not found: {p}")
        return p

    found = _find_alter_file(cwd or Path.cwd())
    if found:
        return found

    raise click.ClickException(
        "No .alter file found in the current directory (or two levels up).\n"
        "Run 'alter init' to create one, or pass --file <path>."
    )


_SKIP_DIRS = frozenset({
    ".venv", "venv", ".env", "__pycache__", ".git",
    "node_modules", "site-packages", ".tox", ".mypy_cache",
})


def _iter_py_files(cwd: Path, limit: int = 500):
    """Yield .py files under *cwd*, skipping virtual-env and cache directories."""
    count = 0
    for py_file in sorted(cwd.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in py_file.parts):
            continue
        yield py_file
        count += 1
        if count >= limit:
            break


def _detect_orm(cwd: Path) -> str:
    """Scan Python files to auto-detect which ORM is used (sqlmodel or sqlalchemy)."""
    py_files = list(_iter_py_files(cwd))
    for py_file in py_files:
        try:
            text = py_file.read_text(errors="ignore")
            if "from sqlmodel" in text or "import sqlmodel" in text:
                return "sqlmodel"
            if "SQLModel" in text:
                return "sqlmodel"
        except OSError:
            continue
    for py_file in py_files:
        try:
            text = py_file.read_text(errors="ignore")
            if "from sqlalchemy" in text or "import sqlalchemy" in text:
                return "sqlalchemy"
        except OSError:
            continue
    return "sqlmodel"  # safe default


def _find_model_dirs(cwd: Path) -> list[Path]:
    """Return candidate model directories to scan."""
    candidates = [
        cwd / "app" / "models",
        cwd / "app",
        cwd / "src",
        cwd,
    ]
    return [d for d in candidates if d.is_dir()]


def _load_demo_schema() -> Path:
    """Copy the bundled SaaS starter demo schema to a temp file and return its path."""
    import shutil
    import tempfile

    demo_src = Path(__file__).parent / "data" / "demo_schema.alter"
    if not demo_src.exists():
        raise click.ClickException(
            "Demo schema not bundled with this installation. "
            "Please reinstall alterdb or file a bug report."
        )

    tmp = tempfile.NamedTemporaryFile(suffix=".alter", delete=False, prefix="alter-demo-")
    tmp.close()
    path = Path(tmp.name)
    shutil.copy2(demo_src, path)
    return path


FILE_OPTION = click.option(
    "--file", "alter_file",
    default=None,
    metavar="PATH",
    help="Path to the .alter file. Auto-detected if omitted.",
)


# ---------------------------------------------------------------------------
# alter (root)
# ---------------------------------------------------------------------------


@click.group()
@click.version_option(package_name="alterdb")
def main() -> None:
    """Alter — understand your database first, design it second."""


# ---------------------------------------------------------------------------
# alter init
# ---------------------------------------------------------------------------


@main.command("init")
@click.option("--orm", "orm_override", default=None, type=click.Choice(["sqlmodel", "sqlalchemy"]),
              help="ORM to use (auto-detected if omitted).")
@click.option("--output", default=None, metavar="PATH",
              help="Output .alter file path (default: <project>.alter in cwd).")
def init(orm_override: str | None, output: str | None) -> None:
    """Create a .alter file from existing ORM model files.

    \b
    alter init                — scan model files and create schema.alter
    alter init --orm sqlmodel — force ORM detection
    """
    cwd = Path.cwd()
    out_path = Path(output) if output else cwd / "schema.alter"

    # From code
    orm = orm_override or _detect_orm(cwd)
    model_dirs = _find_model_dirs(cwd)
    if not model_dirs:
        click.echo("  No model directories found — creating empty schema.")
        from alter.schema import AlterSchema
        schema = AlterSchema(orm=orm)  # type: ignore[arg-type]
        schema.save(out_path)
        click.echo(f"  Created empty {out_path.name}. Add tables with 'alter canvas'.")
        return

    try:
        from alter.parsers.base import get_parser
        parser = get_parser(orm, project_root=cwd)

        # If the primary model directory is a subdirectory (e.g. app/models),
        # scan its parent instead so that sibling files (e.g. app/enums.py)
        # are included in the two-phase pre-scan.
        scan_dir = model_dirs[0]
        if len(model_dirs) > 1 and model_dirs[1] == scan_dir.parent:
            scan_dir = scan_dir.parent

        click.echo(f"  Scanning for {orm} models in {scan_dir.relative_to(cwd)}…")
        result = parser.parse_directory(scan_dir)
    except AlterError as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:
        raise click.ClickException(f"Parser error: {exc}") from exc

    if result.skipped_files:
        for fp in result.skipped_files:
            click.echo(f"  ⚠  Skipped (parse error): {fp.relative_to(cwd)}", err=True)

    result.schema.save(out_path)
    click.echo(
        f"  Created {out_path.name} — "
        f"{len(result.schema.tables)} tables, "
        f"ORM: {orm}"
    )
    if result.warnings:
        for w in result.warnings:
            click.echo(f"  ⚠  {w}", err=True)


def _match_file_paths(schema: "AlterSchema", cwd: Path, orm: str) -> None:  # type: ignore[name-defined]
    """Try to match tables to existing ORM model files by class name / __tablename__."""
    try:
        from alter.parsers.base import get_parser
        parser = get_parser(orm, project_root=cwd)
        for model_dir in _find_model_dirs(cwd):
            result = parser.parse_directory(model_dir)
            name_to_file = {t.name: t.file_path for t in result.schema.tables if t.file_path}
            for tbl in schema.tables:
                if tbl.name in name_to_file:
                    tbl.file_path = name_to_file[tbl.name]
    except Exception:
        pass  # matching is best-effort


# ---------------------------------------------------------------------------
# alter sync
# ---------------------------------------------------------------------------


@main.command("sync")
@FILE_OPTION
@click.option("--dir", "model_dir", default=None, metavar="DIR",
              help="Model files directory (auto-detected if omitted).")
def sync(alter_file: str | None, model_dir: str | None) -> None:
    """Update the .alter file from ORM model files.

    \b
    alter sync    — parse model files, update schema, preserve positions
    """
    path = _require_alter_file(alter_file)

    try:
        from alter.schema import AlterSchema
        current = AlterSchema.load(path)
        pos_map = {t.name: t.position for t in current.tables}
    except AlterError as exc:
        raise click.ClickException(str(exc)) from exc

    cwd = path.parent
    scan_dir = Path(model_dir) if model_dir else None
    if scan_dir is None:
        dirs = _find_model_dirs(cwd)
        scan_dir = dirs[0] if dirs else cwd
    try:
        from alter.parsers.base import get_parser
        parser = get_parser(current.orm, project_root=cwd)
        click.echo(f"  Parsing {current.orm} models in {scan_dir}…")
        result = parser.parse_directory(scan_dir)
        new_schema = result.schema
    except AlterError as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:
        raise click.ClickException(f"Parser error: {exc}") from exc

    if result.skipped_files:
        for fp in result.skipped_files:
            click.echo(f"  ⚠  Skipped: {fp}", err=True)
        sys.exit(1)

    # Preserve canvas positions
    for tbl in new_schema.tables:
        if tbl.name in pos_map:
            tbl.position = pos_map[tbl.name]

    new_schema.save(path)
    click.echo(
        f"  Synced {len(new_schema.tables)} tables → {path.name}"
    )


# ---------------------------------------------------------------------------
# alter add
# ---------------------------------------------------------------------------


@main.command("add")
@click.argument("path", type=click.Path(exists=True))
@FILE_OPTION
def add_cmd(path: str, alter_file: str | None) -> None:
    """Add tables from a model file to the schema.

    Parse PATH for ORM model classes and add any new tables to schema.alter.
    Tables already in the schema are skipped.

    \b
    Examples:
        alter add app/legacy/models.py
        alter add lib/plugins/billing.py --file my.alter
    """
    alter_path = _require_alter_file(alter_file)
    model_file = Path(path).resolve()
    cwd = alter_path.parent

    from alter.schema import AlterSchema
    from alter.parsers.base import get_parser

    schema = AlterSchema.load(alter_path)
    parser = get_parser(schema.orm, project_root=cwd)

    if not parser.detect_orm(model_file):
        raise click.ClickException(
            f"{model_file.name} does not contain {schema.orm} models."
        )

    try:
        # Use parse_file_result to capture enum definitions too — custom enum
        # types on columns would fail schema validation if enums are not added.
        file_result = parser.parse_file_result(model_file)
    except Exception as exc:
        raise click.ClickException(f"Parse error: {exc}") from exc

    tables = file_result.schema.tables
    enums = file_result.schema.enums

    if not tables:
        raise click.ClickException(f"No tables found in {model_file.name}.")

    rel_path = str(model_file.relative_to(cwd))
    for tbl in tables:
        tbl.file_path = rel_path

    existing_names = {t.name for t in schema.tables}
    existing_enums = {e.name for e in schema.enums}
    added = []
    skipped = []
    for tbl in tables:
        if tbl.name in existing_names:
            skipped.append(tbl.name)
        else:
            schema.tables.append(tbl)
            added.append(tbl.name)
    # Always merge new enum definitions (idempotent — skip if already present)
    for enum in enums:
        if enum.name not in existing_enums:
            schema.enums.append(enum)

    schema.save(alter_path)

    if added:
        click.echo(f"  Added {len(added)} table(s) from {rel_path}: {', '.join(added)}")
    if skipped:
        click.echo(f"  Skipped {len(skipped)} (already in schema): {', '.join(skipped)}")
    if not added and not skipped:
        click.echo(f"  No tables found in {rel_path}.")


# ---------------------------------------------------------------------------
# alter apply
# ---------------------------------------------------------------------------


@main.command("apply")
@FILE_OPTION
@click.option("--preview", is_flag=True, default=False,
              help="Print a unified diff without writing any files.")
def apply(alter_file: str | None, preview: bool) -> None:
    """Write the committed schema to ORM model files (surgical update).

    \b
    alter apply           — update model files in place
    alter apply --preview — show what would change without writing
    """
    path = _require_alter_file(alter_file)
    project_root = path.parent

    try:
        from alter.schema import AlterSchema
        from alter.generators.base import get_generator, _default_model_path
        schema = AlterSchema.load(path)
        gen = get_generator(schema.orm)
    except AlterError as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc

    # Group tables by file_path
    file_groups: dict[str, list] = {}
    for t in schema.tables:
        fp = t.file_path or _default_model_path(schema, project_root)
        file_groups.setdefault(fp, []).append(t)

    changed = 0
    for rel_path, tables in sorted(file_groups.items()):
        abs_path = project_root / rel_path
        # Only emit enum classes that belong to this specific file.
        # Enums with file_path=None are treated as local to every file that
        # needs them (single-file projects); enums with an explicit file_path
        # are only emitted in that file — matching the preview_apply behaviour.
        local_enum_names = {
            e.name for e in schema.enums
            if e.file_path is None or e.file_path == rel_path
        }
        file_schema = schema.model_copy(update={"tables": tables})

        try:
            if abs_path.exists():
                existing = abs_path.read_text()
                updated = gen.update_models(file_schema, existing, local_enum_names=local_enum_names)
            else:
                existing = ""
                updated = gen.generate_models(file_schema, local_enum_names=local_enum_names)
        except (AlterError, Exception) as exc:
            click.echo(f"  ✗  {rel_path}: {exc}", err=True)
            sys.exit(1)

        if updated == existing:
            continue

        if preview:
            import difflib
            diff = "\n".join(
                difflib.unified_diff(
                    existing.splitlines(),
                    updated.splitlines(),
                    fromfile=f"a/{rel_path}",
                    tofile=f"b/{rel_path}",
                    lineterm="",
                )
            )
            click.echo(diff)
        else:
            try:
                abs_path.parent.mkdir(parents=True, exist_ok=True)
                abs_path.write_text(updated)
                click.echo(f"  ✓  {rel_path}")
            except OSError as exc:
                click.echo(f"  ✗  Could not write {rel_path}: {exc}", err=True)
                sys.exit(1)
        changed += 1

    if changed == 0:
        click.echo("  No changes — model files are already up to date.")
    elif not preview:
        click.echo(f"\n  Applied to {changed} file{'s' if changed != 1 else ''}.")


# ---------------------------------------------------------------------------
# alter diff
# ---------------------------------------------------------------------------


@main.command("diff")
@FILE_OPTION
@click.option("--format", "fmt", default="text",
              type=click.Choice(["text", "markdown"]),
              help="Output format.")
def diff(alter_file: str | None, fmt: str) -> None:
    """Show differences between the .alter schema and current code.

    \b
    alter diff                     — compare .alter with ORM model files
    alter diff --format markdown   — PR-ready markdown changelog
    """
    path = _require_alter_file(alter_file)

    try:
        from alter.schema import AlterSchema
        from alter.diff import diff_schemas
        current = AlterSchema.load(path)
    except AlterError as exc:
        raise click.ClickException(str(exc)) from exc

    cwd = path.parent
    dirs = _find_model_dirs(cwd)
    scan_dir = dirs[0] if dirs else cwd
    try:
        from alter.parsers.base import get_parser
        parser = get_parser(current.orm, project_root=cwd)
        result = parser.parse_directory(scan_dir)
        code_schema = result.schema
    except (AlterError, Exception) as exc:
        raise click.ClickException(f"Parser error: {exc}") from exc
    changes = diff_schemas(current, code_schema)
    source_label = "code"

    if not changes:
        click.echo(f"  No differences between .alter and {source_label}.")
        return

    if fmt == "markdown":
        _print_diff_markdown(changes)
    else:
        _print_diff_text(changes, source_label)


def _print_diff_text(changes: list, source_label: str) -> None:
    click.echo(f"\n  Changes between .alter and {source_label}:\n")
    for ch in changes:
        icon = "+" if ch.type.startswith("add") else ("~" if ch.type.startswith("modify") else "-")
        col_part = f".{ch.column}" if ch.column else ""
        warn = " ⚠️ destructive" if ch.destructive else ""
        click.echo(f"  {icon}  [{ch.type}] {ch.table}{col_part}{warn}")


def _print_diff_markdown(changes: list) -> None:
    from alter.diff_format import changes_to_markdown  # noqa: PLC0415

    click.echo(changes_to_markdown(changes))


# ---------------------------------------------------------------------------
# alter validate
# ---------------------------------------------------------------------------


@main.command("validate")
@FILE_OPTION
def validate_cmd(alter_file: str | None) -> None:
    """Check the .alter schema for errors and warnings."""
    path = _require_alter_file(alter_file)

    try:
        from alter.schema import AlterSchema
        from alter.validate import validate_schema
        schema = AlterSchema.load(path)
        issues = validate_schema(schema)
    except AlterError as exc:
        raise click.ClickException(str(exc)) from exc

    if not issues:
        click.echo("  ✓  Schema is valid — no issues found.")
        return

    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]
    infos = [i for i in issues if i.severity == "info"]

    icons = {"error": "✗", "warning": "⚠", "info": "ℹ"}
    for issue in issues:
        col = f".{issue.column}" if issue.column else ""
        click.echo(
            f"  {icons[issue.severity]}  [{issue.severity.upper()}] "
            f"{issue.table}{col}: {issue.message}",
            err=(issue.severity == "error"),
        )

    click.echo()
    click.echo(f"  {len(errors)} error(s), {len(warnings)} warning(s), {len(infos)} info(s)")
    if errors:
        sys.exit(1)


# ---------------------------------------------------------------------------
# alter import
# ---------------------------------------------------------------------------


@main.command("import")
@click.argument("source")
@FILE_OPTION
@click.option("--format", "fmt", default=None, type=click.Choice(["sql", "alter"]),
              help="Source format (auto-detected from extension if omitted).")
def import_cmd(source: str, alter_file: str | None, fmt: str | None) -> None:
    """Import tables from a .sql or .alter file into the schema.

    SOURCE can be a file path.  Tables already present are skipped.
    """
    path = _require_alter_file(alter_file)
    src_path = Path(source)

    if not src_path.exists():
        raise click.ClickException(f"Source file not found: {source}")

    # Auto-detect format
    if fmt is None:
        fmt = "alter" if src_path.suffix == ".alter" else "sql"

    try:
        if fmt == "alter":
            from alter.importers.alter_file import import_alter_file
            imported = import_alter_file(src_path)
        else:
            from alter.importers.sql import import_sql
            from alter.schema import AlterSchema
            current_orm = AlterSchema.load(path).orm
            imported = import_sql(src_path.read_text(), orm=current_orm)
    except AlterError as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:
        raise click.ClickException(f"Import error: {exc}") from exc

    from alter.staging import StagingManager
    import copy
    staging = StagingManager(path)

    def apply(schema: "AlterSchema") -> "AlterSchema":  # type: ignore[name-defined]
        s = copy.deepcopy(schema)
        existing_names = {t.name for t in s.tables}
        added = []
        for tbl in imported.tables:
            if tbl.name not in existing_names:
                s.tables.append(copy.deepcopy(tbl))
                added.append(tbl.name)
        for rel in imported.relations:
            if (rel.from_table, rel.from_column) not in {(r.from_table, r.from_column) for r in s.relations}:
                s.relations.append(copy.deepcopy(rel))
        # Also copy enums — tables may reference enum types defined in the source
        existing_enum_names = {e.name for e in s.enums}
        for enum in imported.enums:
            if enum.name not in existing_enum_names:
                s.enums.append(copy.deepcopy(enum))
        return s

    staging.propose(apply)
    staging.commit()
    click.echo(
        f"  Imported {len(imported.tables)} tables from {src_path.name} → {path.name}"
    )


# ---------------------------------------------------------------------------
# alter export
# ---------------------------------------------------------------------------


@main.command("export")
@FILE_OPTION
@click.option("--format", "fmt", default="sql",
              type=click.Choice(["sql", "mermaid", "alter"]),
              help="Output format.")
@click.option("--proposed", is_flag=True, default=False,
              help="Export the proposed (staged) schema.")
@click.option("--output", default=None, metavar="FILE",
              help="Write to this file instead of stdout.")
def export_cmd(alter_file: str | None, fmt: str, proposed: bool, output: str | None) -> None:
    """Export the schema as SQL DDL, Mermaid ERD, or .alter JSON.

    \b
    alter export                       — SQL DDL to stdout
    alter export --format mermaid      — Mermaid ERD
    alter export --format alter        — raw .alter JSON
    alter export --proposed --format mermaid  — export proposed changes
    """
    path = _require_alter_file(alter_file)

    try:
        from alter.staging import StagingManager
        staging = StagingManager(path)
        schema = (
            staging.proposed_schema if (proposed and staging.has_pending())
            else staging.current_schema
        )

        if fmt == "mermaid":
            from alter.exporters.mermaid import export_mermaid
            text = export_mermaid(schema)
        elif fmt == "alter":
            text = schema.model_dump_json(indent=2)
        else:
            from alter.exporters.sql import export_sql
            text = export_sql(schema)
    except AlterError as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc

    if output:
        Path(output).write_text(text)
        click.echo(f"  Exported to {output}")
    else:
        click.echo(text, nl=False)


# ---------------------------------------------------------------------------
# alter canvas
# ---------------------------------------------------------------------------


@main.command("canvas")
@FILE_OPTION
@click.option("--demo", is_flag=True, default=False,
              help="Load the built-in SaaS starter demo schema.")
@click.option("--port", default=8269, show_default=True,
              help="Preferred port for the canvas server.")
@click.option("--no-browser", is_flag=True, default=False,
              help="Start the server without opening a browser tab.")
def canvas(alter_file: str | None, demo: bool, port: int, no_browser: bool) -> None:
    """Open the ERD canvas in your browser."""
    from alter.canvas.server import start_canvas_server

    if demo:
        path = _load_demo_schema()
    elif alter_file:
        path = Path(alter_file)
    else:
        path = _find_alter_file(Path.cwd())
        if path is None:
            path = Path.cwd() / "schema.alter"
            click.echo(
                "  No .alter file found — starting with empty canvas.\n"
                f"  Will save to: {path}\n"
                "  Tip: run with --demo to load a sample schema."
            )
        else:
            click.echo(f"  Schema  →  {path}")

    def on_ready(url: str) -> None:
        if not no_browser:
            def _open() -> None:
                time.sleep(0.4)
                webbrowser.open(url)
            threading.Thread(target=_open, daemon=True).start()

    click.echo()
    start_canvas_server(path, port=port, on_ready=on_ready)


# ---------------------------------------------------------------------------
# alter mcp
# ---------------------------------------------------------------------------


@main.command("mcp")
@FILE_OPTION
def mcp_cmd(alter_file: str | None) -> None:
    """Start the MCP server (stdio transport).

    Add to your AI assistant config::

    \b
        {
          "mcpServers": {
            "alter": {
              "command": "uv",
              "args": ["run", "alter", "mcp", "--file", "path/to/schema.alter"]
            }
          }
        }
    """
    path = _require_alter_file(alter_file)

    try:
        from alter.mcp_server import init_mcp, mcp
        init_mcp(path)
        mcp.run(transport="stdio")
    except AlterError as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:
        raise click.ClickException(f"MCP server error: {exc}") from exc


# ---------------------------------------------------------------------------
# alter merge-driver
# ---------------------------------------------------------------------------


@main.command("merge-driver")
@click.argument("base")
@click.argument("ours")
@click.argument("theirs")
def merge_driver_cmd(base: str, ours: str, theirs: str) -> None:
    """Git merge driver for .alter files.

    Register in .gitattributes::

    \b
        *.alter merge=alter

    And in git config::

    \b
        [merge "alter"]
            name = Alter schema merge driver
            driver = alter merge-driver %O %A %B
    """
    from alter.merge_driver import run_merge_driver
    exit_code = run_merge_driver(base, ours, theirs)
    sys.exit(exit_code)
