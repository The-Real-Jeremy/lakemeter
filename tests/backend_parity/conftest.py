"""Fixtures for the PostgreSQL-vs-SQLite parity suite.

Needs a throwaway PostgreSQL. Point ``LAKEMETER_PARITY_PG_URL`` at it, e.g.::

    LAKEMETER_PARITY_PG_URL='postgresql+psycopg://lakemeter@localhost:5498/lakemeter_pricing?sslmode=require' \
        pytest tests/backend_parity

The suite bootstraps that database itself (schema, functions, prices) and
creates only its own estimates, tagged with a run id. Without the variable the
whole suite is skipped, never silently passed.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

PG_URL = os.getenv("LAKEMETER_PARITY_PG_URL")

pytestmark = pytest.mark.skipif(not PG_URL, reason="LAKEMETER_PARITY_PG_URL is not set")


def pytest_collection_modifyitems(config, items):
    if PG_URL:
        return
    skip = pytest.mark.skip(reason="LAKEMETER_PARITY_PG_URL is not set")
    for item in items:
        if "backend_parity" in str(item.fspath):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def run_id() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture(scope="session")
def pg_engine():
    from sqlalchemy import create_engine
    from app.bootstrap import bootstrap_database

    engine = create_engine(PG_URL, connect_args={"sslmode": "require"})
    bootstrap_database(engine)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def sqlite_file(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("sqlite") / "lakemeter.db"


@pytest.fixture(scope="session")
def sqlite_engine(sqlite_file):
    from app.bootstrap import bootstrap_database
    from app.sqlite_backend import create_sqlite_engine

    engine = create_sqlite_engine(sqlite_file)
    bootstrap_database(engine)
    yield engine
    engine.dispose()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_server(env_overrides: dict, log_path: Path) -> tuple[subprocess.Popen, str]:
    port = _free_port()
    env = {k: v for k, v in os.environ.items()
           if k not in ("DATABASE_URL", "DB_BACKEND", "SQLITE_PATH")}
    env.update({"ENVIRONMENT": "local", "PYTHONPATH": str(BACKEND)}, **env_overrides)
    log = log_path.open("w")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=BACKEND, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 180
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"server exited early, see {log_path}")
        try:
            if httpx.get(f"{base}/api/v1/system/health", timeout=2).status_code == 200:
                return process, base
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    process.terminate()
    raise RuntimeError(f"server did not become healthy, see {log_path}")


@pytest.fixture(scope="session")
def servers(pg_engine, sqlite_engine, sqlite_file, tmp_path_factory):
    """Two live apps: (postgres_base_url, sqlite_base_url)."""
    logs = tmp_path_factory.mktemp("logs")
    pg, pg_base = _start_server(
        {"DATABASE_URL": PG_URL, "LAKEMETER_BOOTSTRAP_DATABASE": "true"},
        logs / "postgres.log")
    sq, sq_base = _start_server(
        {"DB_BACKEND": "sqlite", "SQLITE_PATH": str(sqlite_file)},
        logs / "sqlite.log")
    yield pg_base, sq_base
    for process in (pg, sq):
        process.terminate()
        process.wait(timeout=30)


@pytest.fixture(scope="session")
def clients(servers, run_id):
    headers = {"X-Forwarded-Email": f"parity-{run_id}@example.com"}
    # No keep-alive: after an upstream 500 the server closes the socket, and a
    # reused connection would fail the NEXT request on the client side.
    limits = httpx.Limits(max_keepalive_connections=0)
    pg = httpx.Client(base_url=servers[0] + "/api/v1", headers=headers, timeout=120, limits=limits)
    sq = httpx.Client(base_url=servers[1] + "/api/v1", headers=headers, timeout=120, limits=limits)
    yield pg, sq
    pg.close()
    sq.close()
