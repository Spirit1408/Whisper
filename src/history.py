"""SQLite artifacts and one-way legacy import. Only the DB worker uses sqlite3."""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time
from uuid import uuid4

from presets import BUILTINS, Preset, valid_id, validate

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
def migrate_results(db):
    db.execute("ALTER TABLE artifacts ADD COLUMN current_preset TEXT")
    db.execute("ALTER TABLE artifacts ADD COLUMN last_job TEXT")
    db.execute("ALTER TABLE results RENAME TO results_v1")
    db.execute("""CREATE TABLE results (
          artifact_id TEXT NOT NULL REFERENCES artifacts(id), preset_id TEXT NOT NULL,
          text TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
          manual INTEGER NOT NULL DEFAULT 0, model TEXT, preset_revision INTEGER,
          source_id TEXT NOT NULL, source_revision INTEGER NOT NULL, job_id TEXT,
          PRIMARY KEY(artifact_id,preset_id))""")
    db.execute("""INSERT INTO results(artifact_id,preset_id,text,revision,source_id,source_revision,job_id)
                  SELECT artifact_id,'strict',text,revision,'original',0,artifact_id FROM results_v1""")
    db.execute("UPDATE artifacts SET current_preset='strict' WHERE id IN (SELECT artifact_id FROM results_v1)")
    db.execute("DROP TABLE results_v1")
    db.execute("""CREATE TABLE attempts(job_id TEXT PRIMARY KEY REFERENCES jobs(id),
                  preset_id TEXT NOT NULL, preset_revision INTEGER NOT NULL, model TEXT,
                  source_id TEXT NOT NULL, source_revision INTEGER NOT NULL, expected_revision INTEGER NOT NULL)""")
    db.execute("CREATE TABLE presets(id TEXT PRIMARY KEY, definition TEXT NOT NULL, revision INTEGER NOT NULL)")
    db.execute("PRAGMA user_version=2")


SELECT_ENTRY = """SELECT a.rowid AS cursor, a.id, a.created_at AS ts, a.original_text AS original,
       COALESCE(r.text, a.original_text, a.legacy_text) AS text,
       a.legacy_text, r.preset_id, r.revision, r.manual, j.id AS job_id, j.processing, j.storage, j.delivery, j.error,
       t.preset_id AS attempted_preset, first_job.delivery AS dictation_delivery
       FROM artifacts a LEFT JOIN results r ON r.artifact_id=a.id AND r.preset_id=a.current_preset
       LEFT JOIN jobs j ON j.id=COALESCE(a.last_job,a.id)
       LEFT JOIN jobs first_job ON first_job.id=a.id
       LEFT JOIN attempts t ON t.job_id=j.id WHERE a.trashed=0"""


def valid_preference(key, value):
    if key == "active_preset":
        return valid_id(value)
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
            if version not in (0, 1, 2):
                raise StorageError("unsupported_schema")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            if version == 1:
                # Never overwrite an earlier rollback copy. An interrupted backup has
                # the partial suffix and the live schema is still v1.
                backup = self.path.with_name(self.path.name + ".v1-" + uuid4().hex)
                partial = backup.with_suffix(backup.suffix + ".partial")
                replica = sqlite3.connect(partial)
                try:
                    db.backup(replica)
                    if replica.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise StorageError("backup_failed")
                finally:
                    replica.close()
                partial.rename(backup)
            with db:
                db.execute("BEGIN IMMEDIATE")
                if not version:
                    for ddl in SCHEMA:
                        db.execute(ddl)
                if version < 2:
                    migrate_results(db)
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

    def original(self, job, text, smart, *, auto=False, preset=None, model=""):
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
            if smart:
                chosen = preset or BUILTINS["strict"]
                db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?)",
                           (job, chosen.id, chosen.revision, model, "original", 0, 0))
        self._write(save)

    @staticmethod
    def _attempt(db, job):
        row = db.execute("""SELECT t.*, a.id AS artifact_id,
                CASE t.source_id WHEN 'original' THEN a.original_text WHEN 'legacy' THEN a.legacy_text
                ELSE s.text END AS source FROM attempts t JOIN jobs j ON j.id=t.job_id
                JOIN artifacts a ON a.id=j.artifact_id
                LEFT JOIN results s ON s.artifact_id=a.id AND s.preset_id=t.source_id
                LEFT JOIN results d ON d.artifact_id=a.id AND d.preset_id=t.preset_id
                WHERE t.job_id=? AND j.processing='transforming' AND a.trashed=0
                AND COALESCE(a.last_job,a.id)=j.id AND COALESCE(s.revision,0)=t.source_revision
                AND COALESCE(d.revision,0)=t.expected_revision""", (job,)).fetchone()
        if row is None or row["source"] is None:
            raise StaleAttempt("revision_conflict")
        return row

    def smart_source(self, job):
        return self._call(lambda: self._attempt(self._connection(), job)["source"])

    def start_repeat(self, job, artifact, source, revision, preset, model):
        def begin(db):
            changed = db.execute("UPDATE artifacts SET last_job=? WHERE id=? AND trashed=0", (job, artifact))
            if changed.rowcount != 1:
                raise StaleAttempt("artifact_unavailable")
            dest = db.execute("SELECT revision FROM results WHERE artifact_id=? AND preset_id=?",
                              (artifact, preset.id)).fetchone()
            db.execute("""INSERT INTO jobs(id,artifact_id,processing,storage)
                          SELECT ?,id,'transforming',CASE WHEN current_preset IS NOT NULL THEN 'result'
                          WHEN original_text IS NULL THEN 'legacy' ELSE 'original' END
                          FROM artifacts WHERE id=?""", (job, artifact))
            db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?)",
                       (job, preset.id, preset.revision, model, source, revision, dest[0] if dest else 0))
            self._attempt(db, job)
            return db.execute("SELECT storage FROM jobs WHERE id=?", (job,)).fetchone()[0]
        return self._write(begin)

    def result(self, job, text):
        def save(db):
            db.execute("BEGIN IMMEDIATE")
            t = self._attempt(db, job)
            db.execute("""INSERT INTO results(artifact_id,preset_id,text,revision,manual,model,
                       preset_revision,source_id,source_revision,job_id) VALUES(?,?,?,?,0,?,?,?,?,?)
                       ON CONFLICT(artifact_id,preset_id) DO UPDATE SET text=excluded.text,
                       revision=excluded.revision,manual=0,model=excluded.model,preset_revision=excluded.preset_revision,
                       source_id=excluded.source_id,source_revision=excluded.source_revision,job_id=excluded.job_id""",
                       (t["artifact_id"], t["preset_id"], text, t["expected_revision"]+1, t["model"],
                        t["preset_revision"], t["source_id"], t["source_revision"], job))
            db.execute("UPDATE artifacts SET current_preset=? WHERE id=?", (t["preset_id"], t["artifact_id"]))
            db.execute("UPDATE jobs SET processing='ready', storage='result', error=NULL WHERE id=?", (job,))
        self._write(save)

    def finish(self, job, processing, error=None):
        # A delayed error/cancel must never relabel a completed result, even after restart.
        def finish_attempt(db):
            db.execute("BEGIN IMMEDIATE")
            if processing != "cancelled" and db.execute("SELECT 1 FROM attempts WHERE job_id=?", (job,)).fetchone():
                self._attempt(db, job)
            changed = db.execute("""UPDATE jobs SET processing=?, error=? WHERE id=?
            AND processing IN ('capturing','transcribing','transforming')
            AND (?='cancelled' OR artifact_id IS NULL OR EXISTS
                 (SELECT 1 FROM artifacts WHERE id=jobs.artifact_id AND trashed=0))""",
                                 (processing, error, job, processing))
            if changed.rowcount != 1:
                raise StaleAttempt("stale_attempt")
        self._write(finish_attempt)

    def copied(self, job):
        # An explicit Copy must not erase evidence of an earlier send or ambiguous outcome.
        return self._execute("""UPDATE jobs SET delivery='copied' WHERE id=?
                                AND delivery IN ('skipped','not_requested','copied')""", (job,)) == 1

    def begin_delivery(self, job, storage):
        def begin(db):
            cursor = db.execute("""UPDATE jobs SET delivery='dispatching'
                WHERE id=? AND processing='ready' AND error IS NULL AND storage=? AND delivery='skipped'
                AND EXISTS (SELECT 1 FROM artifacts a WHERE a.id=jobs.artifact_id AND a.trashed=0
                      AND COALESCE(a.last_job,a.id)=jobs.id AND (jobs.storage='original'
                      OR EXISTS (SELECT 1 FROM results r WHERE r.artifact_id=a.id AND r.job_id=jobs.id)))""",
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

    def entry(self, artifact):
        def read():
            row = self._connection().execute(SELECT_ENTRY + " AND a.id=?", (artifact,)).fetchone()
            if row is None:
                raise StaleAttempt("artifact_unavailable")
            return dict(row)
        return self._call(read)

    def versions(self, artifact):
        def read():
            rows = self._connection().execute("""SELECT r.* FROM results r JOIN artifacts a
                    ON r.artifact_id=a.id WHERE a.id=? AND a.trashed=0 ORDER BY r.preset_id""", (artifact,))
            return {row["preset_id"]: dict(row) for row in rows}
        return self._call(read)

    def edit(self, artifact, preset_id, revision, text, source_id):
        if not valid_id(preset_id) or not valid_text(text):
            raise ValueError("invalid_edit")
        def save(db):
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT a.current_preset,a.original_text,j.delivery FROM artifacts a
                      LEFT JOIN jobs j ON j.id=COALESCE(a.last_job,a.id) WHERE a.id=? AND a.trashed=0""",
                             (artifact,)).fetchone()
            if row is None or row["delivery"] == "dispatching":
                raise StaleAttempt("artifact_unavailable")
            job = uuid4().hex
            db.execute("""INSERT INTO jobs(id,artifact_id,processing,storage)
                          VALUES(?,?,'ready','result')""", (job, artifact))
            if source_id in ("original", "legacy"):
                if preset_id != "raw" or (row["original_text"] is None) != (source_id == "legacy"):
                    raise StaleAttempt("invalid_source")
            elif source_id != preset_id or not revision:
                raise StaleAttempt("invalid_source")
            if revision:
                changed = db.execute("""UPDATE results SET text=?,revision=revision+1,manual=1,job_id=?,
                      source_id=?,source_revision=? WHERE artifact_id=? AND preset_id=? AND revision=?""",
                      (text, job, source_id, revision if source_id == preset_id else 0, artifact, preset_id, revision))
            else:
                changed = db.execute("""INSERT INTO results(artifact_id,preset_id,text,revision,manual,
                           source_id,source_revision,job_id) VALUES(?,?,?,1,1,?,0,?)
                           ON CONFLICT(artifact_id,preset_id) DO NOTHING""", (artifact, preset_id, text, source_id, job))
            if changed.rowcount != 1:
                raise StaleAttempt("revision_conflict")
            db.execute("UPDATE artifacts SET current_preset=?, last_job=? WHERE id=?", (preset_id, job, artifact))
        self._write(save)

    def presets(self):
        def read():
            result = dict(BUILTINS)
            for key, definition, revision in self._connection().execute("SELECT id,definition,revision FROM presets"):
                try:
                    result[key] = validate(Preset(id=key, revision=revision, **json.loads(definition)))
                except (ValueError, TypeError, RecursionError):
                    # A damaged override must not silently resolve to a different
                    # model or language. Other definitions remain independently usable.
                    result.pop(key, None)
                    logger.warning("invalid_stored_preset")
            return result
        return self._call(read)

    def save_preset(self, preset, expected):
        preset = validate(replace(preset, revision=expected + 1))
        fields = asdict(preset)
        del fields["id"], fields["revision"]
        def save(db):
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision FROM presets WHERE id=?", (preset.id,)).fetchone()
            prior = row[0] if row else int(preset.id in BUILTINS)
            if prior != expected:
                raise StaleAttempt("definition_changed")
            db.execute("INSERT OR REPLACE INTO presets VALUES(?,?,?)",
                       (preset.id, json.dumps(fields), preset.revision))
        self._write(save)
        return preset

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
