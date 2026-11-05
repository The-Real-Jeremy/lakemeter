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
    def __init__(self, workdir: Path, snapshot: str | None, name: str):
        self.port = _free_port()
        env = {k: v for k, v in os.environ.items()
               if k not in ("DATABASE_URL", "LAKEMETER_SNAPSHOT_PATH")}
        env.update(DB_BACKEND="sqlite", ENVIRONMENT="local", PYTHONPATH=str(BACKEND),
                   SQLITE_PATH=str(workdir / f"{name}-wiped-on-restart.db"))
        if snapshot is not None:
            env["LAKEMETER_SNAPSHOT_PATH"] = snapshot
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
