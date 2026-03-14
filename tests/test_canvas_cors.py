"""Tests for CORS headers on the canvas HTTP server.

The canvas server binds to 127.0.0.1 only, but browser extensions and canvas
UI served from a different local port (e.g. during development) make
cross-origin requests that are blocked without CORS headers.

These tests start a real ``CanvasServer`` instance on port 0 (OS-assigned),
make actual HTTP requests, and assert that the expected headers are present.
"""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from alter.canvas.server import CanvasServer
from alter.schema import AlterSchema


# ---------------------------------------------------------------------------
# Fixture: minimal server on an OS-assigned port
# ---------------------------------------------------------------------------


@pytest.fixture()
def canvas_server(tmp_path: Path):
    """Start a CanvasServer on a free port and tear it down after the test."""
    schema = AlterSchema(orm="sqlmodel")
    alter_file = tmp_path / "schema.alter"
    schema.save(alter_file)

    server = CanvasServer(alter_file, 0)  # port=0 → OS assigns a free port
    port = server.server_address[1]

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    yield port

    server.shutdown()


def _get(port: int, path: str) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    return conn.getresponse()


def _post(port: int, path: str, body: bytes = b"{}") -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(
        "POST",
        path,
        body=body,
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
    )
    return conn.getresponse()


def _options(port: int, path: str) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(
        "OPTIONS",
        path,
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"},
    )
    return conn.getresponse()


# ---------------------------------------------------------------------------
# GET responses carry CORS headers
# ---------------------------------------------------------------------------


def test_get_schema_has_acao_header(canvas_server: int) -> None:
    resp = _get(canvas_server, "/api/schema")
    resp.read()
    assert resp.getheader("Access-Control-Allow-Origin") == "*"


def test_get_schema_has_acam_header(canvas_server: int) -> None:
    resp = _get(canvas_server, "/api/schema")
    resp.read()
    methods = resp.getheader("Access-Control-Allow-Methods") or ""
    assert "GET" in methods
    assert "POST" in methods
    assert "OPTIONS" in methods


def test_get_schema_has_acah_header(canvas_server: int) -> None:
    resp = _get(canvas_server, "/api/schema")
    resp.read()
    headers = resp.getheader("Access-Control-Allow-Headers") or ""
    assert "Content-Type" in headers


def test_get_schema_sql_has_cors(canvas_server: int) -> None:
    resp = _get(canvas_server, "/api/schema-sql")
    resp.read()
    assert resp.getheader("Access-Control-Allow-Origin") == "*"


def test_get_migrate_has_cors(canvas_server: int) -> None:
    resp = _get(canvas_server, "/api/migrate")
    resp.read()
    assert resp.getheader("Access-Control-Allow-Origin") == "*"


def test_get_templates_has_cors(canvas_server: int) -> None:
    resp = _get(canvas_server, "/api/templates")
    resp.read()
    assert resp.getheader("Access-Control-Allow-Origin") == "*"


def test_get_awareness_has_cors(canvas_server: int) -> None:
    resp = _get(canvas_server, "/api/awareness")
    resp.read()
    assert resp.getheader("Access-Control-Allow-Origin") == "*"


def test_static_index_has_cors(canvas_server: int) -> None:
    resp = _get(canvas_server, "/")
    resp.read()
    assert resp.getheader("Access-Control-Allow-Origin") == "*"


# ---------------------------------------------------------------------------
# POST responses carry CORS headers
# ---------------------------------------------------------------------------


def test_post_discard_has_cors(canvas_server: int) -> None:
    resp = _post(canvas_server, "/api/discard")
    resp.read()
    assert resp.getheader("Access-Control-Allow-Origin") == "*"


def test_post_propose_has_cors(canvas_server: int) -> None:
    body = json.dumps({"op": "add_table", "name": "test_tbl", "x": 0, "y": 0}).encode()
    resp = _post(canvas_server, "/api/propose", body)
    resp.read()
    assert resp.getheader("Access-Control-Allow-Origin") == "*"


def test_post_unknown_route_still_has_cors(canvas_server: int) -> None:
    """404 responses from POST must also include CORS headers."""
    resp = _post(canvas_server, "/api/nonexistent")
    resp.read()
    assert resp.getheader("Access-Control-Allow-Origin") == "*"


# ---------------------------------------------------------------------------
# OPTIONS preflight is handled
# ---------------------------------------------------------------------------


def test_options_returns_200(canvas_server: int) -> None:
    resp = _options(canvas_server, "/api/schema")
    resp.read()
    assert resp.status == 200


def test_options_has_acao_header(canvas_server: int) -> None:
    resp = _options(canvas_server, "/api/propose")
    resp.read()
    assert resp.getheader("Access-Control-Allow-Origin") == "*"


def test_options_has_acam_header(canvas_server: int) -> None:
    resp = _options(canvas_server, "/api/propose")
    resp.read()
    methods = resp.getheader("Access-Control-Allow-Methods") or ""
    assert "GET" in methods
    assert "POST" in methods
    assert "OPTIONS" in methods


def test_options_has_acah_header(canvas_server: int) -> None:
    resp = _options(canvas_server, "/api/propose")
    resp.read()
    headers = resp.getheader("Access-Control-Allow-Headers") or ""
    assert "Content-Type" in headers


def test_options_on_unknown_path_returns_200(canvas_server: int) -> None:
    """Preflight for any path (including unmapped) must return 200."""
    resp = _options(canvas_server, "/api/unknown-future-endpoint")
    resp.read()
    assert resp.status == 200
