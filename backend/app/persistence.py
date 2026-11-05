"""Keep SQLite data across Databricks Apps restarts and redeploys.

An app's filesystem is wiped on every restart and redeploy, so on the SQLite
backend the user data (users, estimates, line items, sharing, chat history,
decision records, templates) is copied to durable storage after every write
and copied back on start. Pricing tables are not snapshotted: the bootstrap
rebuilds them from the bundled CSVs on every start.

``LAKEMETER_SNAPSHOT_PATH`` chooses where the snapshot lives:

* ``/Volumes/<catalog>/<schema>/<volume>/.../lakemeter-data.db`` (recommended)
* ``/Workspace/Users/<app service principal>/.../lakemeter-data.db``
* ``file:///some/local/dir/lakemeter-data.db`` (local development and tests)

A path that does not end in ``.db`` is a folder, and ``lakemeter-data.db`` is
used inside it; that is what an Apps UC volume resource (``valueFrom``) gives.
A three-part volume name (``catalog.schema.volume``) also works.

Safety rule: if a snapshot exists but cannot be restored, the app keeps
running but never uploads, so an empty session can never overwrite good data.
The UI shows a red banner in that state (``GET /api/v1/system/persistence``).
"""
from __future__ import annotations

import io
import logging
import os
import re
import sqlite3
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

USER_TABLES = (
    "users", "templates", "estimates", "line_items", "sharing",
    "conversation_messages", "decision_records",
)
_META = "lakemeter_snapshot_meta"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class PersistenceStatus:
    enabled: bool = False
    snapshot_path: Optional[str] = None
    restore: str = "not_attempted"  # restored | no_snapshot | failed | disabled
    restore_detail: Optional[str] = None
    restored_rows: dict = field(default_factory=dict)
    uploads_blocked: bool = False
    last_snapshot_at: Optional[str] = None
    last_snapshot_rows: dict = field(default_factory=dict)
    last_error: Optional[str] = None
    last_error_at: Optional[str] = None
    snapshots_written: int = 0

    @property
    def healthy(self) -> bool:
        if not self.enabled:
            return True
        return self.restore in ("restored", "no_snapshot") and self.last_error is None

    def as_dict(self) -> dict:
        data = asdict(self)
        data["healthy"] = self.healthy
        return data


# ---------------------------------------------------------------------------
# Storage targets
# ---------------------------------------------------------------------------

class _Store:
    def read(self) -> Optional[bytes]:
        """Snapshot bytes, or None when no snapshot exists yet."""
        raise NotImplementedError

    def write(self, data: bytes) -> None:
        raise NotImplementedError


class _LocalStore(_Store):
    def __init__(self, path: str):
        self.path = Path(path)

    def read(self) -> Optional[bytes]:
        return self.path.read_bytes() if self.path.exists() else None

    def write(self, data: bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        partial = self.path.with_suffix(self.path.suffix + ".partial")
        partial.write_bytes(data)
        partial.replace(self.path)


def _is_not_found(error: Exception) -> bool:
    text = f"{type(error).__name__} {error}".lower()
    return any(marker in text for marker in (
        "notfound", "not_found", "not found", "does not exist", "resource_does_not_exist",
        "404"))


class _VolumeStore(_Store):
    """A file in a Unity Catalog volume, through the Files API."""

    def __init__(self, path: str):
        from databricks.sdk import WorkspaceClient
        self.path = path
        self.client = WorkspaceClient()

    def read(self) -> Optional[bytes]:
        try:
            return self.client.files.download(self.path).contents.read()
        except Exception as error:  # noqa: BLE001 - classified below
            if _is_not_found(error):
                return None
            raise

    def write(self, data: bytes) -> None:
        directory = self.path.rsplit("/", 1)[0]
        self.client.files.create_directory(directory)
        self.client.files.upload(self.path, io.BytesIO(data), overwrite=True)


class _WorkspaceStore(_Store):
    """A workspace file, e.g. in the app service principal's home folder."""

    def __init__(self, path: str):
        from databricks.sdk import WorkspaceClient
        self.path = path
        self.client = WorkspaceClient()

    def read(self) -> Optional[bytes]:
        try:
            with self.client.workspace.download(self.path) as handle:
                return handle.read()
        except Exception as error:  # noqa: BLE001
            if _is_not_found(error):
                return None
            raise

    def write(self, data: bytes) -> None:
        from databricks.sdk.service.workspace import ImportFormat
        self.client.workspace.mkdirs(self.path.rsplit("/", 1)[0])
        self.client.workspace.upload(self.path, io.BytesIO(data),
                                     format=ImportFormat.AUTO, overwrite=True)


def _store_for(path: str) -> _Store:
    if path.startswith("file://"):
        return _LocalStore(path[len("file://"):])
    if path.startswith("/Volumes/"):
        return _VolumeStore(path)
    if path.startswith("/Workspace/"):
        return _WorkspaceStore(path)
    raise ValueError(
        "LAKEMETER_SNAPSHOT_PATH must start with /Volumes/, /Workspace/ or file://")


# ---------------------------------------------------------------------------
# Snapshot and restore
# ---------------------------------------------------------------------------

def _columns(connection, schema: str, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f"PRAGMA {schema}.table_info({table})")]


def build_snapshot(engine) -> tuple[bytes, dict]:
    """A consistent copy of the user tables as a standalone SQLite file."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "snapshot.db"
        raw = engine.raw_connection()
        try:
            connection = raw.driver_connection
            connection.execute("ATTACH DATABASE ? AS snap", (str(target),))
            try:
                counts = {}
                connection.execute("BEGIN")  # one read transaction = one point in time
                for table in USER_TABLES:
                    connection.execute(
                        f"CREATE TABLE snap.{table} AS SELECT * FROM lakemeter.{table}")
                    counts[table] = connection.execute(
                        f"SELECT COUNT(*) FROM snap.{table}").fetchone()[0]
                connection.execute(f"CREATE TABLE snap.{_META} (key TEXT, value TEXT)")
                connection.executemany(f"INSERT INTO snap.{_META} VALUES (?, ?)", [
                    ("created_at", _now()),
                    ("app_version", _app_version()),
                ])
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.execute("DETACH DATABASE snap")
        finally:
            raw.close()
        return target.read_bytes(), counts


def restore_snapshot(engine, data: bytes) -> dict:
    """Replace the user tables with the snapshot's rows. Returns row counts."""
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "snapshot.db"
        source.write_bytes(data)
        check = sqlite3.connect(str(source))
        try:
            result = check.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError(f"snapshot integrity check failed: {result}")
        finally:
            check.close()
        raw = engine.raw_connection()
        try:
            connection = raw.driver_connection
            connection.execute("ATTACH DATABASE ? AS snap", (str(source),))
            try:
                counts = {}
                connection.execute("BEGIN IMMEDIATE")
                present = {row[0] for row in connection.execute(
                    "SELECT name FROM snap.sqlite_master WHERE type = 'table'")}
                # Children first when clearing, parents first when loading.
                for table in reversed(USER_TABLES):
                    connection.execute(f"DELETE FROM lakemeter.{table}")
                for table in USER_TABLES:
                    if table not in present:
                        counts[table] = 0
                        continue
                    live = _columns(connection, "lakemeter", table)
                    saved = set(_columns(connection, "snap", table))
                    shared = [c for c in live if c in saved]  # tolerate schema growth
                    column_list = ", ".join(shared)
                    connection.execute(
                        f"INSERT INTO lakemeter.{table} ({column_list}) "
                        f"SELECT {column_list} FROM snap.{table}")
                    counts[table] = connection.execute(
                        f"SELECT COUNT(*) FROM lakemeter.{table}").fetchone()[0]
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.execute("DETACH DATABASE snap")
        finally:
            raw.close()
        return counts


def _app_version() -> str:
    from app.version import APP_VERSION
    return APP_VERSION


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class PersistenceManager:
    def __init__(self):
        self.status = PersistenceStatus()
        self._store: Optional[_Store] = None
        self._engine = None
        self._lock = threading.Lock()
        self._dirty = threading.Event()

    def configure(self, engine) -> None:
        from app.sqlite_backend import is_sqlite_backend

        path = os.getenv("LAKEMETER_SNAPSHOT_PATH", "").strip().rstrip("/")
        if re.fullmatch(r"[^/.\s]+\.[^/.\s]+\.[^/.\s]+", path):
            # A UC volume's three-part name (what the App Catalog resolves a volume
            # resource to): <catalog>.<schema>.<volume> -> /Volumes/<catalog>/<schema>/<volume>
            path = "/Volumes/" + path.replace(".", "/")
        if path and not path.endswith(".db"):
            # An Apps UC volume resource (valueFrom) injects the volume root.
            path = f"{path}/lakemeter-data.db"
        self._engine = engine
        if not is_sqlite_backend():
            self.status = PersistenceStatus(enabled=False, restore="disabled",
                                            restore_detail="PostgreSQL backend: not needed")
            return
        if not path:
            self.status = PersistenceStatus(
                enabled=True, restore="failed", uploads_blocked=True,
                restore_detail=("LAKEMETER_SNAPSHOT_PATH is not set: nothing is saved "
                                "and all estimates are lost when the app restarts"))
            logger.error(self.status.restore_detail)
            return
        self.status = PersistenceStatus(enabled=True, snapshot_path=path)
        try:
            self._store = _store_for(path)
        except Exception as error:  # noqa: BLE001
            self._fail_restore(f"cannot use snapshot path: {error}")
            return
        self._watch_commits(engine)

    def _watch_commits(self, engine) -> None:
        from sqlalchemy import event

        @event.listens_for(engine, "commit")
        def _mark_dirty(_connection):
            self._dirty.set()

    def _fail_restore(self, detail: str) -> None:
        self.status.restore = "failed"
        self.status.restore_detail = detail
        self.status.uploads_blocked = True
        logger.error("Snapshot restore failed, uploads are blocked: %s", detail)

    def restore(self) -> None:
        if not self.status.enabled or self._store is None or self.status.restore == "failed":
            return
        try:
            data = self._store.read()
        except Exception as error:  # noqa: BLE001
            self._fail_restore(f"could not read {self.status.snapshot_path}: {error}")
            return
        if data is None:
            self.status.restore = "no_snapshot"
            self.status.restore_detail = "no snapshot yet; one is written on the first save"
            logger.info("No snapshot at %s yet", self.status.snapshot_path)
            return
        try:
            self.status.restored_rows = restore_snapshot(self._engine, data)
        except Exception as error:  # noqa: BLE001
            self._fail_restore(f"snapshot at {self.status.snapshot_path} is unreadable: {error}")
            return
        self.status.restore = "restored"
        self.status.restore_detail = f"restored {len(data):,} bytes"
        self._dirty.clear()
        logger.info("Restored snapshot: %s", self.status.restored_rows)

    def snapshot_if_dirty(self) -> None:
        """Called after each write request; blocks until the upload finishes."""
        if not self._dirty.is_set():
            return
        self.snapshot()

    def snapshot(self) -> None:
        if not self.status.enabled or self._store is None or self.status.uploads_blocked:
            return
        with self._lock:
            self._dirty.clear()
            try:
                data, counts = build_snapshot(self._engine)
                self._store.write(data)
            except Exception as error:  # noqa: BLE001
                self._dirty.set()
                self.status.last_error = str(error)
                self.status.last_error_at = _now()
                logger.error("Snapshot upload failed: %s", error)
                return
            self.status.last_snapshot_at = _now()
            self.status.last_snapshot_rows = counts
            self.status.last_error = None
            self.status.last_error_at = None
            self.status.snapshots_written += 1


persistence = PersistenceManager()
