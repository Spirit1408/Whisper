"""SQLite artifacts and one-way legacy import. Only the DB worker uses sqlite3."""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time

logger = logging.getLogger(__name__)
LEGACY_DIR = Path(__file__).resolve().parents[1]


class StorageError(Exception):
    """Safe application-level error, without data or personal paths."""


class StaleAttempt(StorageError):
    """The target was discarded or the attempt already finished."""


@dataclass(frozen=True)
class ImportReport:
    status: str
    imported: int = 0
    existing: int = 0
    rejected: int = 0
    reasons: tuple = ()


SCHEMA = (
    """CREATE TABLE artifacts (
          id TEXT PRIMARY KEY NOT NULL, created_at REAL, original_text TEXT,
          legacy_text TEXT, trashed INTEGER NOT NULL DEFAULT 0,
          CHECK ((original_text IS NULL) != (legacy_text IS NULL)))""",
    """CREATE TRIGGER immutable_source BEFORE UPDATE OF original_text, legacy_text ON artifacts
       BEGIN SELECT RAISE(ABORT, 'immutable_source'); END""",
    """CREATE TABLE results (artifact_id TEXT PRIMARY KEY REFERENCES artifacts(id),
          text TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1))""",
    """CREATE TABLE jobs (id TEXT PRIMARY KEY NOT NULL, artifact_id TEXT REFERENCES artifacts(id),
          processing TEXT NOT NULL, storage TEXT NOT NULL DEFAULT 'none',
          delivery TEXT NOT NULL DEFAULT 'not_requested', error TEXT)""",
    """CREATE TABLE legacy_imports (source TEXT PRIMARY KEY, digest TEXT NOT NULL,
          accepted INTEGER NOT NULL, rejected INTEGER NOT NULL, reasons TEXT NOT NULL)""",
    """CREATE TABLE preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
)
SELECT_ENTRY = """SELECT a.rowid AS cursor, a.id, a.created_at AS ts, a.original_text AS original,
       COALESCE(r.text, a.original_text, a.legacy_text) AS text,
       j.processing, j.storage, j.delivery, j.error
       FROM artifacts a LEFT JOIN results r ON r.artifact_id=a.id
       LEFT JOIN jobs j ON j.id=a.id WHERE a.trashed=0"""


def valid_preference(key, value):
    if key == "delivery_mode":
        return value in ("artifact", "auto")
    if key == "lm_studio":
        return isinstance(value, bool)
    if key in ("lm_model", "audio_device"):
        return ((key == "audio_device" and value is None) or
                isinstance(value, str) and bool(value.strip()) and valid_text(value))
    return False


def valid_text(value):
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def timestamp(value):
    try:
        if type(value) in (float, int):
            datetime.fromtimestamp(value)
            return value
    except (OverflowError, ValueError, OSError):
        pass
    return None


class HistoryStore:
    def __init__(self, path=None, legacy_dir=LEGACY_DIR):
        if path is None:
            base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
            path = base / "LocalAIInputWorkspace" / "artifacts.sqlite3"
        self.path = Path(path)
        self._owner = ThreadPoolExecutor(max_workers=1, thread_name_prefix="artifact-store")
        self._gate = threading.Lock()
        self._closed = False
        self._db = None
        self.reports = {}
        try:
            self._call(self._open)
        except StorageError:
            self.reports["database"] = ImportReport("unavailable")
            return
        for source in ("history", "state"):
            try:
                self.reports[source] = self._call(self._import, source, Path(legacy_dir))
            except StorageError:
                self.reports[source] = ImportReport("import_failed")

    def _call(self, operation, *args):
        with self._gate:
            if self._closed:
                raise StorageError("closed")
            future = self._owner.submit(operation, *args)
        try:
            return future.result()
        except (OSError, sqlite3.Error, UnicodeError) as exc:
            logger.warning("artifact_store_failed code=%s", getattr(exc, "sqlite_errorcode", 0))
            raise StorageError("storage_error") from None

    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=0.5)
        db.row_factory = sqlite3.Row
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise StorageError("unsupported_schema")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                db.execute("BEGIN IMMEDIATE")
                if not version:
                    for ddl in SCHEMA:
                        db.execute(ddl)
                    db.execute("PRAGMA user_version=1")
                db.execute("""UPDATE jobs SET processing='interrupted'
                              WHERE processing IN ('capturing','transcribing','transforming')""")
                db.execute("UPDATE jobs SET delivery='unknown' WHERE delivery IN ('dispatching','paste_dispatched')")
        except BaseException:
            db.close()
            raise
        self._db = db

    def _connection(self):
        if self._db is None:
            raise StorageError("unavailable")
        return self._db

    def _import(self, source, directory):
        db = self._connection()
        try:
            data = (directory / (source + ".json")).read_bytes()
        except FileNotFoundError:
            return ImportReport("absent")
        digest = sha256(data).hexdigest()
        previous = db.execute("SELECT * FROM legacy_imports WHERE source=?", (source,)).fetchone()
        if previous:
            if previous["digest"] != digest:
                return ImportReport("source_changed")
            try:
                reasons = tuple(map(tuple, json.loads(previous["reasons"])))
            except (TypeError, ValueError, RecursionError):
                raise StorageError("invalid_import_metadata") from None
            return ImportReport("already_imported", existing=previous["accepted"],
                                rejected=previous["rejected"], reasons=reasons)
        try:
            packet = json.loads(data.decode("utf-8-sig"))
        except (ValueError, UnicodeError, RecursionError):
            return ImportReport("invalid_json")
        expected = list if source == "history" else dict
        if not isinstance(packet, expected):
            return ImportReport("invalid_root")
        reasons = Counter()
        with db:
            db.execute("BEGIN IMMEDIATE")
            accepted = 0
            existing = 0
            if source == "history":
                # rowid is the paging cursor. Legacy files are newest first.
                for position in range(len(packet) - 1, -1, -1):
                    item = packet[position]
                    if not isinstance(item, dict) or not valid_text(item.get("text")):
                        reasons["invalid_entry"] += 1
                        continue
                    inserted = db.execute("INSERT INTO artifacts(id,created_at,legacy_text) VALUES(?,?,?)",
                                          (f"legacy-{digest}-{position}", timestamp(item.get("ts")), item["text"]))
                    accepted += inserted.rowcount
            else:
                for key, value in packet.items():
                    if key in ("audio_device", "lm_studio", "lm_model") and valid_preference(key, value):
                        inserted = db.execute("INSERT OR IGNORE INTO preferences VALUES(?,?)",
                                              (key, json.dumps(value)))
                        accepted += inserted.rowcount
                        existing += 1 - inserted.rowcount
                    else:
                        reasons["unsupported_preference"] += 1
            rejected = sum(reasons.values())
            if accepted + existing + rejected != len(packet):
                raise StorageError("import_count_mismatch")
            db.execute("INSERT INTO legacy_imports VALUES(?,?,?,?,?)",
                       (source, digest, accepted + existing, rejected, json.dumps(sorted(reasons.items()))))
        return ImportReport("imported", imported=accepted, existing=existing, rejected=rejected,
                            reasons=tuple(sorted(reasons.items())))

    def _write(self, operation, *args):
        def transaction():
            db = self._connection()
            with db:
                return operation(db, *args)
        return self._call(transaction)

    def start(self, job, processing="transcribing", *, auto=False):
        self._execute("""INSERT INTO jobs(id,processing,delivery) VALUES(?,?,?)
                         ON CONFLICT(id) DO UPDATE SET processing=excluded.processing
                         WHERE jobs.processing='capturing'""", (job, processing, "skipped" if auto else "not_requested"))

    def original(self, job, text, smart, *, auto=False):
        def save(db):
            row = db.execute("SELECT original_text FROM artifacts WHERE id=?", (job,)).fetchone()
            if row:
                if row[0] != text:
                    raise StorageError("original_conflict")
                return
            db.execute("INSERT INTO artifacts(id,created_at,original_text) VALUES(?,?,?)",
                       (job, time.time(), text))
            # May succeed even if the earlier metadata-only transaction failed.
            changed = db.execute("""INSERT INTO jobs VALUES(?,?,?,'original',?,NULL)
                          ON CONFLICT(id) DO UPDATE SET artifact_id=excluded.artifact_id,
                          processing=excluded.processing, storage='original', delivery=excluded.delivery, error=NULL
                          WHERE jobs.processing IN ('capturing', 'transcribing') AND jobs.storage='none'""",
                       (job, job, "transforming" if smart else "ready", "skipped" if auto else "not_requested"))
            if changed.rowcount != 1:
                raise StaleAttempt("stale_attempt")
        self._write(save)

    def smart_source(self, job):
        def read():
            row = self._connection().execute("""SELECT a.original_text FROM jobs j
                JOIN artifacts a ON a.id=j.artifact_id WHERE j.id=? AND j.processing='transforming'
                AND j.storage='original' AND a.trashed=0 AND a.original_text IS NOT NULL""", (job,)).fetchone()
            if row is None:
                raise StaleAttempt("stale_attempt")
            return row[0]
        return self._call(read)

    def result(self, job, text):
        def save(db):
            changed = db.execute("""UPDATE jobs SET processing='ready', storage='result', error=NULL
                WHERE id=? AND processing='transforming' AND storage='original'
                AND EXISTS(SELECT 1 FROM artifacts WHERE id=? AND trashed=0)""", (job, job))
            if changed.rowcount != 1:
                raise StaleAttempt("stale_attempt")
            db.execute("INSERT INTO results VALUES(?,?,1)", (job, text))
        self._write(save)

    def finish(self, job, processing, error=None):
        # A delayed error/cancel must never relabel a completed result, even after restart.
        changed = self._execute("""UPDATE jobs SET processing=?, error=? WHERE id=?
            AND processing IN ('capturing','transcribing','transforming')
            AND (?='cancelled' OR artifact_id IS NULL OR EXISTS
                 (SELECT 1 FROM artifacts WHERE id=jobs.artifact_id AND trashed=0))""",
                                (processing, error, job, processing))
        if changed != 1:
            raise StaleAttempt("stale_attempt")

    def copied(self, job):
        # An explicit Copy must not erase evidence of an earlier send or ambiguous outcome.
        return self._execute("""UPDATE jobs SET delivery='copied' WHERE id=?
                                AND delivery IN ('skipped','not_requested','copied')""", (job,)) == 1

    def begin_delivery(self, job, storage):
        def begin(db):
            cursor = db.execute("""UPDATE jobs SET delivery='dispatching'
                WHERE id=? AND processing='ready' AND error IS NULL AND storage=? AND delivery='skipped'
                AND EXISTS (SELECT 1 FROM artifacts WHERE id=jobs.artifact_id AND trashed=0)""",
                                (job, storage))
            if cursor.rowcount != 1:
                raise StaleAttempt("delivery_unavailable")
        self._write(begin)

    def end_delivery(self, job, status):
        if status not in ("confirmed", "skipped", "paste_dispatched", "unknown"):
            raise ValueError("invalid_delivery")
        def finish(db):
            cursor = db.execute("UPDATE jobs SET delivery=? WHERE id=? AND delivery='dispatching'",
                                (status, job))
            if cursor.rowcount != 1:
                raise StaleAttempt("delivery_unavailable")
        self._write(finish)

    def _execute(self, sql, args):
        def execute(db):
            return db.execute(sql, args).rowcount
        return self._write(execute)

    def page(self, before=None, limit=50):
        def read():
            sql = SELECT_ENTRY
            args = []
            if before is not None:
                sql += " AND a.rowid < ?"
                args.append(before)
            rows = self._connection().execute(sql + " ORDER BY a.rowid DESC LIMIT ?", [*args, limit])
            return [dict(row) for row in rows]
        return self._call(read)

    def preferences(self):
        def read():
            result = {}
            for key, stored in self._connection().execute("SELECT * FROM preferences"):
                try:
                    value = json.loads(stored)
                except (ValueError, TypeError, RecursionError):
                    continue
                if valid_preference(key, value):
                    result[key] = value
            return result
        return self._call(read)

    def set_preference(self, key, value):
        if not valid_preference(key, value):
            raise ValueError("unsupported_preference")
        self._execute("INSERT OR REPLACE INTO preferences VALUES(?,?)", (key, json.dumps(value)))

    def remove(self, entry_id):
        self._execute("UPDATE artifacts SET trashed=1 WHERE id=?", (entry_id,))

    def close(self):
        with self._gate:
            if self._closed:
                return
            self._closed = True
            self._owner.submit(self._close)
        self._owner.shutdown(wait=True)

    def _close(self):
        if self._db is not None:
            self._db.close()
            self._db = None
