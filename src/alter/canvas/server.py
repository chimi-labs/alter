"""Local HTTP server for the ERD canvas.

Serves the static canvas UI and a small JSON API:

  GET  /api/schema           — current + proposed schema state as JSON
  GET  /api/schema-sql       — full CREATE TABLE DDL for the effective schema
  POST /api/position         — persist table drag position to the .alter file
  POST /api/propose          — apply a schema change (add/edit/drop table or column)
  POST /api/commit           — commit proposed → current, write to disk
  POST /api/discard          — throw away proposed schema
  POST /api/undo             — undo last proposal
  POST /api/redo             — redo last undone proposal
  GET  /api/migrate          — SQL migration preview for pending changes
  GET  /api/templates        — list of built-in template names
  POST /api/template         — load a template into proposed schema
  POST /api/paste-sql        — parse pasted CREATE TABLE SQL into proposed schema
  POST /api/apply-to-code    — write committed schema to ORM model files (alter apply)
  POST /api/sync-from-code   — re-parse model files, update schema.alter (alter sync)
  GET  /api/events           — SSE stream (schema_changed / position_updated / file_changed)
  GET  /api/awareness        — detect untracked/unmapped tables for smart nudges
  GET  /                     — index.html
  GET  /style.css            — stylesheet
  GET  /canvas.js            — canvas JavaScript

Security: binds to 127.0.0.1 only. Never 0.0.0.0.
"""

from __future__ import annotations

import copy
import json
import queue
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable

from alter.diff import SchemaChange, diff_schemas
from alter.exporters.sql import export_sql
from alter.importers.sql import import_sql

from alter.schema import AlterSchema, Column, EnumDef, Relation, Table
from alter.staging import StagingManager

_STATIC = Path(__file__).parent / "static"
_TEMPLATES = Path(__file__).parent.parent / "templates"

_GRID_COLS = 3
_GRID_COL_W = 290
_GRID_ROW_H = 310
_GRID_ORIGIN_X = 80
_GRID_ORIGIN_Y = 80

# How long after a server-side file write to suppress watchfiles events (seconds).
_SELF_WRITE_SUPPRESS_S = 0.5


# ---------------------------------------------------------------------------
# Threading HTTP server
# ---------------------------------------------------------------------------


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    """Multi-threaded HTTP server — required for concurrent SSE + API requests."""

    daemon_threads = True


# ---------------------------------------------------------------------------
# Auto-layout helpers
# ---------------------------------------------------------------------------


def _needs_layout(schema: AlterSchema) -> bool:
    """Return True when every table is still at the default (0, 0) position."""
    return bool(schema.tables) and all(
        t.position.x == 0 and t.position.y == 0 for t in schema.tables
    )


def _auto_position(schema: AlterSchema) -> None:
    """Assign a simple grid layout in-place when no positions are set."""
    for i, table in enumerate(schema.tables):
        col = i % _GRID_COLS
        row = i // _GRID_COLS
        table.position.x = _GRID_ORIGIN_X + col * _GRID_COL_W
        table.position.y = _GRID_ORIGIN_Y + row * _GRID_ROW_H


def _schema_to_json(schema: AlterSchema, layout_auto: bool = False) -> dict:
    raw = json.loads(schema.model_dump_json(indent=2))
    raw["layout_auto"] = layout_auto
    return raw


def _migration_sql(staging: StagingManager) -> str:
    """Generate ALTER TABLE migration SQL for pending diff changes."""
    if not staging.has_pending():
        return ""
    changes = staging.get_diff()
    if not changes:
        return ""

    cur = staging.current_schema
    prop = staging.proposed_schema
    lines: list[str] = []

    from alter.types import alter_to_sql
    from alter.exporters.sql import _column_to_sql, _table_to_sql
    from alter.schema import Relation as Rel

    cur_tables = {t.name: t for t in cur.tables}
    prop_tables = {t.name: t for t in prop.tables}

    for ch in changes:
        if ch.type == "add_table":
            tbl = prop_tables.get(ch.table)
            if tbl:
                rel_index: dict = {}
                for r in prop.relations:
                    rel_index.setdefault((r.from_table, r.from_column), []).append(r)
                lines.append(_table_to_sql(tbl, rel_index) + "\n")

        elif ch.type == "drop_table":
            lines.append(f"DROP TABLE {ch.table};\n")

        elif ch.type == "add_column":
            tbl = prop_tables.get(ch.table)
            if tbl and ch.column:
                col = next((c for c in tbl.columns if c.name == ch.column), None)
                if col:
                    rel_index = {(r.from_table, r.from_column): r for r in prop.relations}
                    col_sql = _column_to_sql(col)
                    lines.append(f"ALTER TABLE {ch.table} ADD COLUMN {col_sql};\n")

        elif ch.type == "drop_column":
            lines.append(f"ALTER TABLE {ch.table} DROP COLUMN {ch.column};\n")

        elif ch.type == "modify_column":
            tbl = prop_tables.get(ch.table)
            if tbl and ch.column:
                col = next((c for c in tbl.columns if c.name == ch.column), None)
                if col:
                    sql_type = alter_to_sql(col.type, col.max_length)
                    lines.append(
                        f"ALTER TABLE {ch.table} ALTER COLUMN {ch.column} TYPE {sql_type};\n"
                    )

        elif ch.type == "add_relation":
            rel = ch.details.get("relation")
            if rel:
                on_del = f" ON DELETE {rel['on_delete']}" if rel.get("on_delete") else ""
                constraint = f"fk_{rel['from_table']}_{rel['from_column']}_{rel['to_table']}"
                lines.append(
                    f"ALTER TABLE {rel['from_table']} ADD CONSTRAINT "
                    f"{constraint} "
                    f"FOREIGN KEY ({rel['from_column']}) "
                    f"REFERENCES {rel['to_table']} ({rel['to_column']})"
                    f"{on_del};\n"
                )

        elif ch.type == "drop_relation":
            rel = ch.details.get("relation")
            if rel:
                lines.append(
                    f"ALTER TABLE {rel['from_table']} DROP CONSTRAINT "
                    f"fk_{rel['from_table']}_{rel['from_column']};\n"
                )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Schema payload helper (shared by HTTP responses + SSE broadcasts)
# ---------------------------------------------------------------------------


def _build_schema_payload(staging: StagingManager) -> dict:
    """Build the schema JSON payload with the same shape as /api/schema."""
    raw = _schema_to_json(staging.current_schema, layout_auto=False)
    if staging.has_pending():
        prop = staging.proposed_schema
        raw["proposed_schema"] = _schema_to_json(prop)
        changes = staging.get_diff()
        raw["pending_count"] = len(changes)
        raw["changes"] = [
            {
                "type": c.type,
                "table": c.table,
                "column": c.column,
                "destructive": c.destructive,
            }
            for c in changes
        ]
    else:
        raw["proposed_schema"] = None
        raw["pending_count"] = 0
        raw["changes"] = []
    return raw


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    """Request handler for the canvas server."""

    server: "CanvasServer"  # type: ignore[assignment]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # suppress default noisy logging

    # ── Routing ─────────────────────────────────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        routes: dict[str, Callable[[], None]] = {
            "/api/schema":      self._serve_schema,
            "/api/schema-sql":  self._serve_schema_sql,
            "/api/migrate":     self._serve_migrate,
            "/api/templates":   self._serve_templates,
            "/api/events":      self._serve_events,
            "/api/awareness":   self._serve_awareness,
            "/":              lambda: self._serve_static("index.html", "text/html; charset=utf-8"),
            "/index.html":    lambda: self._serve_static("index.html", "text/html; charset=utf-8"),
            "/style.css":     lambda: self._serve_static("style.css", "text/css; charset=utf-8"),
            "/canvas.js":     lambda: self._serve_static("canvas.js", "application/javascript; charset=utf-8"),
        }
        handler = routes.get(path)
        if handler:
            handler()
        else:
            self._send(404, b"Not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        routes: dict[str, Callable[[bytes], None]] = {
            "/api/position":        self._update_position,
            "/api/propose":         self._handle_propose,
            "/api/commit":          self._handle_commit,
            "/api/discard":         self._handle_discard,
            "/api/undo":            self._handle_undo,
            "/api/redo":            self._handle_redo,
            "/api/template":        self._handle_template,
            "/api/paste-sql":       self._handle_paste_sql,
            "/api/apply-to-code":   self._handle_apply_to_code,
            "/api/sync-from-code":  self._handle_sync_from_code,
        }
        handler = routes.get(path)
        if handler:
            handler(body)
        else:
            self._send(404, b"Not found", "text/plain")

    # ── GET handlers ────────────────────────────────────────────────────────

    def _serve_schema(self) -> None:
        staging = self.server.staging
        cur = staging.current_schema
        layout_auto = _needs_layout(cur)
        if layout_auto:
            _auto_position(cur)

        raw = _schema_to_json(cur, layout_auto)

        # Include proposed schema and diff summary if pending
        if staging.has_pending():
            prop = staging.proposed_schema
            prop_raw = _schema_to_json(prop)
            raw["proposed_schema"] = prop_raw
            changes = staging.get_diff()
            raw["pending_count"] = len(changes)
            raw["changes"] = [
                {
                    "type": c.type,
                    "table": c.table,
                    "column": c.column,
                    "destructive": c.destructive,
                }
                for c in changes
            ]
        else:
            raw["proposed_schema"] = None
            raw["pending_count"] = 0
            raw["changes"] = []

        self._send(200, json.dumps(raw).encode(), "application/json")

    def _serve_schema_sql(self) -> None:
        """Return full CREATE TABLE DDL for the effective schema (proposed if pending)."""
        staging = self.server.staging
        schema = staging.proposed_schema if staging.has_pending() else staging.current_schema
        sql = export_sql(schema)
        self._send(200, json.dumps({"sql": sql}).encode(), "application/json")

    def _serve_migrate(self) -> None:
        sql = _migration_sql(self.server.staging)
        self._send(200, json.dumps({"sql": sql}).encode(), "application/json")

    def _serve_templates(self) -> None:
        names = []
        if _TEMPLATES.exists():
            names = [p.stem for p in sorted(_TEMPLATES.glob("*.alter"))]
        self._send(200, json.dumps({"templates": names}).encode(), "application/json")

    def _serve_events(self) -> None:
        """Long-lived SSE endpoint. Streams schema_changed / position_updated / file_changed."""
        q: queue.Queue[bytes] = queue.Queue(maxsize=64)
        srv = self.server

        with srv._sse_lock:
            srv._sse_clients.append(q)

        # Keep connection alive — do NOT let the base class close it.
        self.close_connection = False

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        try:
            # Send an initial comment so the browser knows the connection is live.
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()

            while True:
                try:
                    data = q.get(timeout=25)  # block up to 25 s before sending a heartbeat
                    self.wfile.write(data)
                    self.wfile.flush()
                except queue.Empty:
                    # Heartbeat comment — keeps proxies / browser from timing out.
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # client disconnected — clean up in finally
        finally:
            with srv._sse_lock:
                try:
                    srv._sse_clients.remove(q)
                except ValueError:
                    pass

    def _serve_awareness(self) -> None:
        """Return tables that exist in model files but not in .alter (untracked),
        and tables in .alter that have no matching model file (unmapped)."""
        staging = self.server.staging
        schema = staging.effective_schema()
        project_dir = self.server._path.parent

        untracked: list[str] = []
        unmapped: list[str] = []

        try:
            # Unmapped: tables whose file_path either isn't set or the file doesn't exist.
            schema_table_names = {t.name for t in schema.tables}
            for tbl in schema.tables:
                fp = tbl.file_path
                if not fp or not (project_dir / fp).exists():
                    unmapped.append(tbl.name)

            # Untracked: parse Python files referenced by the schema and look for
            # table classes that aren't in the .alter schema yet.
            if schema.orm in ("sqlmodel", "sqlalchemy"):
                scanned_files: set[str] = set()
                for tbl in schema.tables:
                    if tbl.file_path and tbl.file_path not in scanned_files:
                        scanned_files.add(tbl.file_path)
                        fpath = project_dir / tbl.file_path
                        if fpath.exists():
                            try:
                                if schema.orm == "sqlmodel":
                                    from alter.parsers.sqlmodel import SQLModelParser
                                    parsed = SQLModelParser().parse_file(fpath)
                                else:
                                    from alter.parsers.sqlalchemy import SQLAlchemyParser
                                    parsed = SQLAlchemyParser().parse_file(fpath)
                                for t in parsed:
                                    if t.name not in schema_table_names:
                                        untracked.append(t.name)
                            except Exception:
                                pass  # parse errors are non-fatal for awareness
        except Exception:
            pass  # awareness is always best-effort

        self._send(
            200,
            json.dumps({"untracked": untracked, "unmapped": unmapped}).encode(),
            "application/json",
        )

    # ── POST handlers ────────────────────────────────────────────────────────

    def _update_position(self, body: bytes) -> None:
        try:
            payload = json.loads(body)
            table_name = str(payload["table"])
            x = int(payload["x"])
            y = int(payload["y"])
            self.server.save_position(table_name, x, y)
            self._send(200, b'{"ok":true}', "application/json")
            # Broadcast the new position to other SSE clients (multi-tab awareness).
            self.server.broadcast(
                "position_updated", {"table_name": table_name, "x": x, "y": y}
            )
        except Exception as exc:
            self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")

    def _handle_propose(self, body: bytes) -> None:
        """Apply a schema change to the proposed schema.

        Body: { "op": "add_table"|"drop_table"|"add_column"|"drop_column"|
                       "modify_column"|"add_relation"|"drop_relation",
                ... op-specific fields ... }
        """
        try:
            payload = json.loads(body)
            op = payload.get("op", "")
            staging = self.server.staging

            def apply(schema: AlterSchema) -> AlterSchema:
                s = copy.deepcopy(schema)
                if op == "add_table":
                    name = payload["name"]
                    if not any(t.name == name for t in s.tables):
                        tbl = Table(name=name)
                        tbl.position.x = int(payload.get("x", 80))
                        tbl.position.y = int(payload.get("y", 80))
                        # Seed with a default uuid PK so new tables are valid.
                        # The user can delete it or rename it from the canvas.
                        tbl.columns.append(Column(
                            name="id",
                            type="uuid",
                            primary_key=True,
                            nullable=False,
                            default="uuid4",
                        ))
                        s.tables.append(tbl)

                elif op == "drop_table":
                    name = payload["name"]
                    s.tables = [t for t in s.tables if t.name != name]
                    s.relations = [
                        r for r in s.relations
                        if r.from_table != name and r.to_table != name
                    ]

                elif op == "add_column":
                    tname = payload["table"]
                    col_data = payload["column"]
                    tbl = next((t for t in s.tables if t.name == tname), None)
                    if tbl:
                        col = Column(**col_data)
                        tbl.columns.append(col)

                elif op == "drop_column":
                    tname = payload["table"]
                    cname = payload["column"]
                    tbl = next((t for t in s.tables if t.name == tname), None)
                    if tbl:
                        tbl.columns = [c for c in tbl.columns if c.name != cname]

                elif op == "modify_column":
                    tname = payload["table"]
                    cname = payload["column"]
                    updates = payload.get("updates", {})
                    tbl = next((t for t in s.tables if t.name == tname), None)
                    if tbl:
                        col = next((c for c in tbl.columns if c.name == cname), None)
                        if col:
                            for k, v in updates.items():
                                if hasattr(col, k):
                                    setattr(col, k, v)

                elif op == "add_relation":
                    rel_data = payload["relation"]
                    s.relations.append(Relation(**rel_data))
                    # Also stamp the source column's foreign_key field so the
                    # FK badge renders immediately in the canvas.
                    _ft = rel_data.get("from_table")
                    _fc = rel_data.get("from_column")
                    _tt = rel_data.get("to_table")
                    _tc = rel_data.get("to_column")
                    if _ft and _fc and _tt and _tc:
                        _tbl = next((t for t in s.tables if t.name == _ft), None)
                        if _tbl:
                            _col = next((c for c in _tbl.columns if c.name == _fc), None)
                            if _col:
                                _col.foreign_key = f"{_tt}.{_tc}"

                elif op == "drop_relation":
                    rname = payload["name"]
                    s.relations = [r for r in s.relations if r.name != rname]

                elif op == "add_enum":
                    ename = payload["name"]
                    values = payload.get("values", [])
                    if not any(e.name == ename for e in s.enums):
                        s.enums.append(EnumDef(name=ename, values=values))

                elif op == "edit_enum":
                    ename = payload["name"]
                    updates = payload.get("updates", {})
                    enum = next((e for e in s.enums if e.name == ename), None)
                    if enum:
                        new_name = updates.get("name")
                        if new_name and new_name != ename:
                            enum.name = new_name
                        if "values" in updates:
                            enum.values = EnumDef(
                                name=enum.name,
                                values=updates["values"],
                            ).values  # run normalise_values validator

                elif op == "drop_enum":
                    ename = payload["name"]
                    s.enums = [e for e in s.enums if e.name != ename]

                return s

            staging.propose(apply)
            self._send_schema_response()
        except Exception as exc:
            self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")

    def _handle_commit(self, body: bytes) -> None:
        try:
            # Mark the upcoming disk write as server-initiated so the file
            # watcher doesn't re-broadcast it as an external change.
            self.server._last_self_write = time.monotonic()
            self.server.staging.commit()
            self._send_schema_response()
        except Exception as exc:
            self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")

    def _handle_discard(self, body: bytes) -> None:
        self.server.staging.discard()
        self._send_schema_response()

    def _handle_undo(self, body: bytes) -> None:
        self.server.staging.undo()
        self._send_schema_response()

    def _handle_redo(self, body: bytes) -> None:
        self.server.staging.redo()
        self._send_schema_response()

    def _handle_template(self, body: bytes) -> None:
        try:
            payload = json.loads(body)
            name = payload.get("name", "")
            path = _TEMPLATES / f"{name}.alter"
            if not path.exists():
                self._send(404, json.dumps({"error": "Template not found"}).encode(), "application/json")
                return
            template_schema = AlterSchema.load(path)
            staging = self.server.staging

            def apply(schema: AlterSchema) -> AlterSchema:
                s = copy.deepcopy(schema)
                existing_names = {t.name for t in s.tables}
                for tbl in template_schema.tables:
                    if tbl.name not in existing_names:
                        s.tables.append(copy.deepcopy(tbl))
                existing_rels = {(r.from_table, r.from_column) for r in s.relations}
                for rel in template_schema.relations:
                    if (rel.from_table, rel.from_column) not in existing_rels:
                        s.relations.append(copy.deepcopy(rel))
                # Position imported tables that have no positions
                _auto_position_new(s)
                return s

            staging.propose(apply)
            self._send_schema_response()
        except Exception as exc:
            self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")

    def _handle_paste_sql(self, body: bytes) -> None:
        try:
            payload = json.loads(body)
            sql = payload.get("sql", "")
            if not sql.strip():
                self._send(400, json.dumps({"error": "No SQL provided"}).encode(), "application/json")
                return
            parsed = import_sql(sql, orm=self.server.staging.current_schema.orm or "sqlmodel")
            staging = self.server.staging

            def apply(schema: AlterSchema) -> AlterSchema:
                s = copy.deepcopy(schema)
                existing_names = {t.name for t in s.tables}
                for tbl in parsed.tables:
                    if tbl.name not in existing_names:
                        s.tables.append(copy.deepcopy(tbl))
                existing_rels = {(r.from_table, r.from_column) for r in s.relations}
                for rel in parsed.relations:
                    if (rel.from_table, rel.from_column) not in existing_rels:
                        s.relations.append(copy.deepcopy(rel))
                _auto_position_new(s)
                return s

            staging.propose(apply)
            self._send_schema_response()
        except Exception as exc:
            self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")

    def _handle_apply_to_code(self, body: bytes) -> None:
        """Write committed schema to ORM model files (alter apply)."""
        try:
            from alter.mcp_server import _apply_to_code_impl
            preview = False
            if body:
                data = json.loads(body)
                preview = data.get("preview", False)
            result = _apply_to_code_impl(
                self.server.staging, self.server._path.parent, preview=preview
            )
            self._send(200, json.dumps({"message": result}).encode(), "application/json")
        except Exception as exc:
            self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")

    def _handle_sync_from_code(self, body: bytes) -> None:
        """Re-parse model files and update schema.alter (alter sync)."""
        try:
            from alter.mcp_server import _sync_from_code_impl
            # Mark as server-initiated so the file watcher doesn't re-broadcast.
            self.server._last_self_write = time.monotonic()
            _sync_from_code_impl(
                self.server.staging,
                self.server._path.parent,
                alter_file=self.server._path,
            )
            self._send_schema_response()
        except Exception as exc:
            self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _send_schema_response(self) -> None:
        """Build schema payload, send as HTTP response, and broadcast via SSE."""
        raw = _build_schema_payload(self.server.staging)
        self._send(200, json.dumps(raw).encode(), "application/json")
        # Notify all connected SSE clients (other tabs, AI assistant canvas, etc.)
        self.server.broadcast("schema_changed", raw)

    def _serve_static(self, name: str, mime: str) -> None:
        p = _STATIC / name
        if not p.exists():
            self._send(404, b"Not found", "text/plain")
            return
        self._send(200, p.read_bytes(), mime)

    def _send(self, code: int, body: bytes, mime: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------------------
# Position helpers
# ---------------------------------------------------------------------------


def _auto_position_new(schema: AlterSchema) -> None:
    """Give grid positions to any tables that are still at (0,0)."""
    positioned = [(t.position.x, t.position.y) for t in schema.tables if not (t.position.x == 0 and t.position.y == 0)]
    i = len(positioned)
    for table in schema.tables:
        if table.position.x == 0 and table.position.y == 0:
            col = i % _GRID_COLS
            row = i // _GRID_COLS
            table.position.x = _GRID_ORIGIN_X + col * _GRID_COL_W
            table.position.y = _GRID_ORIGIN_Y + row * _GRID_ROW_H
            i += 1


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class CanvasServer(_ThreadingHTTPServer):
    """Multi-threaded HTTP server that holds canvas state and SSE broadcast."""

    def __init__(self, alter_file_path: Path, port: int) -> None:
        self._path = alter_file_path
        self.staging = StagingManager(alter_file_path)

        # SSE client management
        self._sse_clients: list[queue.Queue[bytes]] = []
        self._sse_lock = threading.Lock()

        # Timestamp of the last server-side .alter file write, used to
        # suppress spurious watchfiles events caused by our own commits.
        self._last_self_write: float = 0.0

        super().__init__(("127.0.0.1", port), _Handler)

        # Start the background file watcher AFTER the server is bound.
        self._start_file_watcher()

    # ── SSE broadcast ────────────────────────────────────────────────────────

    def broadcast(self, event_type: str, payload: dict) -> None:
        """Push an SSE event to every connected client.

        The message format follows the SSE spec:
            data: {"type": "schema_changed", ...}\n\n

        Clients whose queue is full (slow consumers) are dropped silently.
        """
        msg = (
            "data: "
            + json.dumps({"type": event_type, **payload})
            + "\n\n"
        ).encode()

        with self._sse_lock:
            dead: list[queue.Queue[bytes]] = []
            for q in self._sse_clients:
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._sse_clients.remove(q)

    # ── File watcher ────────────────────────────────────────────────────────

    def _start_file_watcher(self) -> None:
        """Watch the .alter file for external changes and broadcast file_changed events.

        Uses watchfiles with a 200 ms debounce.  Writes made by the server
        itself (commit, save_position) are suppressed via _last_self_write.
        """
        path = self._path

        def _watch() -> None:
            try:
                from watchfiles import watch as wf_watch
            except ImportError:
                return  # watchfiles not installed — live-sync disabled

            for _changes in wf_watch(str(path), debounce=200):
                # Skip if the change was caused by the server writing the file.
                if time.monotonic() - self._last_self_write < _SELF_WRITE_SUPPRESS_S:
                    continue

                # Reload the schema from disk and broadcast.
                try:
                    self.staging.current_schema = AlterSchema.load(path)
                except Exception:
                    continue  # corrupt or incomplete write — skip

                payload = _build_schema_payload(self.staging)
                self.broadcast("file_changed", payload)

        threading.Thread(target=_watch, daemon=True, name="alter-file-watcher").start()

    # ── Position + persistence ───────────────────────────────────────────────

    def save_position(self, table_name: str, x: int, y: int) -> None:
        """Update one table's position in the current schema and persist to disk.

        Also mirrors the update into proposed_schema (when pending) so that
        subsequent propose() calls don't snap the table back to its pre-drag
        position.
        """
        for schema in filter(None, [self.staging.current_schema, self.staging.proposed_schema]):
            for table in schema.tables:
                if table.name == table_name:
                    table.position.x = x
                    table.position.y = y
                    break

        # Mark the write as server-initiated before touching the file.
        self._last_self_write = time.monotonic()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.staging.current_schema.save(self._path)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def start_canvas_server(
    alter_file_path: Path,
    port: int = 8269,
    on_ready: Callable[[str], None] | None = None,
) -> None:
    """Start the canvas server.

    Tries *port* first; if busy, increments up to +9.  Calls *on_ready* with
    the URL once the socket is bound (before entering the serve loop).
    """
    server: CanvasServer | None = None
    actual_port = port

    for p in range(port, port + 10):
        try:
            server = CanvasServer(alter_file_path, p)
            actual_port = p
            break
        except OSError:
            continue

    if server is None:
        raise RuntimeError(
            f"Could not bind to any port in range {port}–{port + 9}. "
            "Kill the process using that port and try again."
        )

    url = f"http://127.0.0.1:{actual_port}"
    print(f"  Canvas  →  {url}")
    print("  Press Ctrl-C to stop.\n")

    if on_ready:
        on_ready(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Canvas server stopped.")
    finally:
        server.server_close()
