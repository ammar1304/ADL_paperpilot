"""Shared fixtures.

Integration tests that need a real Postgres read TEST_DATABASE_URL and are
skipped when it is not set. Point it at a throwaway database: the tests delete
rows from `runs`.
"""

import os
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def free_port() -> int:
    """A local TCP port with nothing listening on it."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _ReadyzHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        code = 200 if self.path == "/readyz" else 404
        self.send_response(code)
        self.end_headers()
        self.wfile.write(b"all shards are ready" if code == 200 else b"")

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def fake_qdrant() -> Iterator[str]:
    """A minimal HTTP server that answers like Qdrant's /readyz. Not a real Qdrant."""
    server = HTTPServer(("127.0.0.1", 0), _ReadyzHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def test_db_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set; skipping Postgres integration test")
    return url


def make_client(db_url: str, qdrant_url: str, db_wait_s: float = 5.0) -> TestClient:
    settings = Settings(
        APP_DATABASE_URL=db_url,
        QDRANT_URL=qdrant_url,
        LOG_LEVEL="WARNING",
        HEALTH_TIMEOUT_S=1.0,
        STARTUP_DB_WAIT_S=db_wait_s,
    )
    return TestClient(create_app(settings))


@pytest.fixture
def client(test_db_url: str, fake_qdrant: str) -> Iterator[TestClient]:
    with make_client(test_db_url, fake_qdrant) as c:
        c.app.state.db.ensure_schema()
        with c.app.state.db.pool.connection() as conn:
            conn.execute("DELETE FROM runs")
        yield c
