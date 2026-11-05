"""SQLite persistence across restarts, the way Databricks Apps restarts work.

Each "restart" is a new server process with a NEW, empty SQLite path (the
Apps filesystem is wiped), pointed at the same snapshot. Uses a file:// store
so it needs no workspace; the /Volumes and /Workspace stores share the same
code path apart from the read/write calls.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

BACKEND = Path(__file__).resolve().parents[2] / "backend"
HEADERS = {"X-Forwarded-Email": "persist@example.com"}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class App:
    def __init__(self, workdir: Path, snapshot: str | None, name: str, extra_env: dict | None = None):
        self.port = _free_port()
        env = {k: v for k, v in os.environ.items()
               if k not in ("DATABASE_URL", "LAKEMETER_SNAPSHOT_PATH")}
        env.update(DB_BACKEND="sqlite", ENVIRONMENT="local", PYTHONPATH=str(BACKEND),
                   SQLITE_PATH=str(workdir / f"{name}-wiped-on-restart.db"))
        if snapshot is not None:
            env["LAKEMETER_SNAPSHOT_PATH"] = snapshot
        env.update(extra_env or {})
        self.log = workdir / f"{name}.log"
        self.process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(self.port)],
            cwd=BACKEND, env=env, stdout=self.log.open("w"), stderr=subprocess.STDOUT)
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{self.port}/api/v1",
                                   headers=HEADERS, timeout=60)
        deadline = time.time() + 120
        while time.time() < deadline:
            assert self.process.poll() is None, self.log.read_text()
            try:
                if self.client.get("/system/health").status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        raise RuntimeError(self.log.read_text())

    def persistence(self) -> dict:
        return self.client.get("/system/persistence").json()

    def stop(self) -> None:
        self.process.terminate()
        self.process.wait(timeout=30)


@pytest.fixture
def workdir(tmp_path):
    return tmp_path


def _create_estimate(app: App, name: str) -> str:
    created = app.client.post("/estimates/", json=dict(
        estimate_name=name, cloud="aws", region="us-east-1", tier="PREMIUM"))
    assert created.status_code == 201, created.text
    estimate_id = created.json()["estimate_id"]
    item = app.client.post("/line-items/", json=dict(
        estimate_id=estimate_id, workload_name="nightly", workload_type="JOBS",
        driver_node_type="i3.xlarge", worker_node_type="i3.xlarge", num_workers=2,
        runs_per_day=2, avg_runtime_minutes=30))
    assert item.status_code == 201, item.text
    return estimate_id


def test_estimate_survives_restart_with_wiped_disk(workdir):
    snapshot = workdir / "durable" / "lakemeter-data.db"
    first = App(workdir, f"file://{snapshot}", "first")
    try:
        assert first.persistence()["restore"] == "no_snapshot"
        estimate_id = _create_estimate(first, "survives")
        # The snapshot is written before the write request returns.
        assert snapshot.exists()
        status = first.persistence()
        assert status["healthy"] and status["snapshots_written"] >= 1
        assert status["last_snapshot_rows"]["line_items"] == 1
        before = first.client.get(f"/estimates/{estimate_id}/full").json()
    finally:
        first.stop()

    second = App(workdir, f"file://{snapshot}", "second")
    try:
        status = second.persistence()
        assert status["restore"] == "restored", status
        assert status["restored_rows"]["estimates"] == 1
        after = second.client.get(f"/estimates/{estimate_id}/full")
        assert after.status_code == 200
        assert after.json() == before
        # Edits after the restore are saved too.
        renamed = second.client.put(f"/estimates/{estimate_id}", json={"estimate_name": "renamed"})
        assert renamed.status_code == 200
    finally:
        second.stop()

    third = App(workdir, f"file://{snapshot}", "third")
    try:
        assert third.client.get(f"/estimates/{estimate_id}").json()["estimate_name"] == "renamed"
    finally:
        third.stop()


def test_corrupt_snapshot_blocks_uploads_and_is_never_overwritten(workdir):
    snapshot = workdir / "durable" / "lakemeter-data.db"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(b"this is not a sqlite database")
    original = snapshot.read_bytes()
    app = App(workdir, f"file://{snapshot}", "corrupt")
    try:
        status = app.persistence()
        assert status["restore"] == "failed" and status["uploads_blocked"] and not status["healthy"]
        _create_estimate(app, "must not overwrite")
        assert snapshot.read_bytes() == original
        assert app.persistence()["snapshots_written"] == 0
    finally:
        app.stop()


def test_missing_snapshot_path_is_reported_not_silent(workdir):
    app = App(workdir, None, "unset")
    try:
        status = app.persistence()
        assert status["enabled"] and not status["healthy"]
        assert "LAKEMETER_SNAPSHOT_PATH is not set" in status["restore_detail"]
    finally:
        app.stop()


def test_failed_upload_is_reported(workdir):
    blocker = workdir / "not-a-directory"
    blocker.write_text("a file where the snapshot directory should be")
    app = App(workdir, f"file://{blocker}/lakemeter-data.db", "unwritable")
    try:
        assert app.persistence()["restore"] == "no_snapshot"
        _create_estimate(app, "upload fails")
        status = app.persistence()
        assert status["last_error"] and not status["healthy"]
    finally:
        app.stop()


@pytest.mark.parametrize("given, expected", [
    ("/Volumes/main/apps/snapshots", "/Volumes/main/apps/snapshots/lakemeter-data.db"),
    ("main.apps.snapshots", "/Volumes/main/apps/snapshots/lakemeter-data.db"),
    ("/Volumes/main/apps/snapshots/custom.db", "/Volumes/main/apps/snapshots/custom.db"),
    ("file:///tmp/x/lakemeter-data.db", "file:///tmp/x/lakemeter-data.db"),
])
def test_snapshot_path_forms(monkeypatch, given, expected):
    """An Apps volume resource injects /Volumes/<c>/<s>/<v>; App-ception injects
    the three-part name. Both must land on the same file."""
    sys.path.insert(0, str(BACKEND))
    import app.persistence as persistence
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    monkeypatch.setenv("LAKEMETER_SNAPSHOT_PATH", given)
    monkeypatch.setattr(persistence, "_store_for", lambda path: persistence._LocalStore("/nonexistent"))
    manager = persistence.PersistenceManager()
    monkeypatch.setattr(manager, "_watch_commits", lambda engine: None)
    manager.configure(engine=object())
    assert manager.status.snapshot_path == expected


# ---------------------------------------------------------------------------
# LAKEMETER_PERSISTENCE=user_volume and none
# ---------------------------------------------------------------------------

class _FakeFilesApi:
    """The Files API for one volume: token "good" may read and write, anything else gets 403."""

    def __init__(self):
        import http.server
        import threading

        files, calls = {}, []
        self.files, self.calls = files, calls

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _answer(self, code, body=b""):
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _allowed(self):
                token = self.headers.get("Authorization", "")
                calls.append((self.command, self.path.split("?")[0], token))
                if token != "Bearer good":
                    self._answer(403, b'{"error_code":"PERMISSION_DENIED"}')
                    return False
                return True

            def do_GET(self):
                if self._allowed():
                    key = self.path.split("?")[0]
                    self._answer(200, files[key]) if key in files else self._answer(404)

            def do_PUT(self):
                if self._allowed():
                    body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                    if self.path.startswith("/api/2.0/fs/files"):
                        files[self.path.split("?")[0]] = body
                    self._answer(204)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def files_api():
    api = _FakeFilesApi()
    yield api
    api.close()


def _user_env(files_api) -> dict:
    return {"LAKEMETER_PERSISTENCE": "user_volume", "DATABRICKS_HOST": files_api.url,
            "LAKEMETER_AI_ASSISTANT": "off", "DATABRICKS_CLIENT_ID": "", "DATABRICKS_CLIENT_SECRET": ""}


def _as(token: str) -> dict:
    return {**HEADERS, "X-Forwarded-Access-Token": token}


def test_user_volume_saves_and_restores_with_the_users_token(workdir, files_api):
    snapshot_key = "/api/2.0/fs/files/Volumes/main/lakemeter/snapshots/lakemeter-data.db"
    first = App(workdir, "main.lakemeter.snapshots", "user-first", _user_env(files_api))
    try:
        status = first.persistence()
        assert status["mode"] == "user_volume" and status["restore"] == "pending_user"
        # No token yet: writes are refused, so a later load cannot overwrite them.
        refused = first.client.post("/estimates/", json=dict(
            estimate_name="x", cloud="aws", region="us-east-1", tier="PREMIUM"))
        assert refused.status_code == 503, refused.text
        # A user without the grant: still waiting, told why, writes still refused.
        denied = first.client.post("/estimates/", headers=_as("bad"), json=dict(
            estimate_name="x", cloud="aws", region="us-east-1", tier="PREMIUM"))
        assert denied.status_code == 503 and "READ VOLUME and WRITE VOLUME" in denied.text
        assert first.persistence()["restore"] == "pending_user"
        # A user with the grant: loads (nothing yet), then the save goes up as that user.
        first.client.headers.update(_as("good"))
        estimate_id = _create_estimate(first, "saved as the user")
        status = first.persistence()
        assert status["restore"] == "no_snapshot" and status["healthy"], status
        assert status["snapshots_written"] >= 2 and snapshot_key in files_api.files
        assert all(token == "Bearer good" for method, path, token in files_api.calls
                   if method == "PUT")
    finally:
        first.stop()

    second = App(workdir, "main.lakemeter.snapshots", "user-second", _user_env(files_api))
    try:
        second.client.headers.update(_as("good"))
        status = second.persistence()
        assert status["restore"] == "restored" and status["restored_rows"]["estimates"] == 1
        restored = second.client.get(f"/estimates/{estimate_id}")
        assert restored.status_code == 200 and restored.json()["estimate_name"] == "saved as the user"
    finally:
        second.stop()


def test_persistence_none_still_calculates_and_says_so(workdir):
    app = App(workdir, None, "none", {"LAKEMETER_PERSISTENCE": "none"})
    try:
        status = app.persistence()
        assert status["mode"] == "none" and not status["healthy"] and status["uploads_blocked"]
        assert "LAKEMETER_PERSISTENCE=none" in status["restore_detail"]
        estimate_id = _create_estimate(app, "kept until restart")
        assert app.client.get(f"/estimates/{estimate_id}").status_code == 200
    finally:
        app.stop()
