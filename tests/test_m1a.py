"""M1a reliability tests on synthetic artifacts, PCM, and private SQLite databases."""
import json
from contextlib import closing
import sqlite3
import subprocess
import sys
import threading
from unittest.mock import Mock, patch

from test_m0 import IsolatedApp, RAW, EDIT, response, choice


class ReliableOriginalTests(IsolatedApp):
    def committed_rows(self, sql, args=()):
        # An independent reader sees only real commits, including while the GUI is idle.
        with closing(sqlite3.connect(self.database)) as db:
            return db.execute(sql, args).fetchall()

    def test_commit_visible_before_smart_with_window_hidden(self):
        self.app.window.hide()
        self.app.bridge.transcribed.disconnect(self.app.window.add_transcription)
        def backend(*args, **kwargs):
            self.assertEqual(self.committed_rows("SELECT original_text FROM artifacts"), [(RAW,)])
            self.assertEqual(self.committed_rows("SELECT processing,storage,delivery FROM jobs"),
                             [("transforming", "original", "skipped")])
            return response(choice(EDIT))
        self.http.side_effect = backend
        self.process()
        self.http.assert_called_once()
        self.assertEqual(self.committed_rows("SELECT original_text,text FROM artifacts JOIN results "
                                  "ON artifacts.id=results.artifact_id"), [(RAW, EDIT)])
        self.assertEqual(self.app.window.text_edit.toPlainText(), "")

    def test_smart_failure_and_timeout_keep_original_on_restart(self):
        import requests
        from history import HistoryStore
        for error in (requests.ConnectionError, requests.Timeout):
            self.http.side_effect = error()
            self.process()
        ids = [entry["id"] for entry in self.app.history.page()]
        self.app.history.close()
        restarted = HistoryStore(self.database, self.root)
        self.addCleanup(restarted.close)
        for row in restarted.page():
            self.assertEqual((row["original"], row["text"], row["processing"], row["storage"],
                              row["delivery"]), (RAW, RAW, "fallback", "original", "skipped"))
        self.assertEqual([row["id"] for row in restarted.page()], ids)
        self.assertEqual(len(ids), 2)
        self.send.assert_not_called()

    def test_db_enforces_immutable_original_and_one_current_result(self):
        from history import StorageError
        self.process()
        row = self.app.history.page()[0]
        for cmd in ("UPDATE artifacts SET original_text='replacement'",
                    "UPDATE artifacts SET legacy_text='replacement'"):
            with self.assertRaises(StorageError):
                self.app.history._execute(cmd, ())
        self.app.history.original(row["id"], RAW, True)
        with self.assertRaises(StorageError):
            self.app.history.original(row["id"], EDIT, True)
        with self.assertRaises(StorageError):
            self.app.history.result(row["id"], "late result")
        self.assertEqual(self.app.history.page(), [row])

    def test_real_sqlite_full_retains_previous_data_and_ram_text(self):
        self.process(False)
        store = self.app.history
        pages = self.committed_rows("PRAGMA page_count")[0][0]
        store._execute(f"PRAGMA max_page_count={pages}", ())
        large = RAW * 5000
        with patch.object(self.app.transcriber, "transcribe", return_value=large):
            self.process()
        self.assertEqual([e["text"] for e in store.page()], [RAW])
        self.assertEqual(self.app.jobs.ram_results()[0].text, large)
        self.assertEqual(self.app.window.text_edit.toPlainText(), large)
        self.assertIn("not saved", self.app.window.result_label.text())
        self.http.assert_not_called()
        self.send.assert_not_called()

    def test_deferred_constraint_failure_at_commit(self):
        store = self.app.history
        store._execute("CREATE TABLE guard (id TEXT REFERENCES artifacts(id) DEFERRABLE INITIALLY DEFERRED)", ())
        store._execute("""CREATE TRIGGER deferred_failure AFTER INSERT ON artifacts
                           BEGIN INSERT INTO guard VALUES('missing'); END""", ())
        self.process()
        self.assertEqual(self.committed_rows("SELECT count(*) FROM artifacts"), [(0,)])
        self.assertEqual(self.committed_rows("SELECT count(*) FROM guard"), [(0,)])
        self.assertEqual(self.committed_rows("SELECT processing,storage,error FROM jobs"),
                         [("skipped", "none", "original_write_failed")])
        self.http.assert_not_called()
        self.assertEqual(self.app.jobs.ram_results()[0].text, RAW)
        self.assertIn("not saved", self.app.window.result_label.text())

    def test_recover_unsaved_result_after_next_dictation(self):
        self.app.history._execute("""CREATE TRIGGER no_results BEFORE INSERT ON results
                                  BEGIN SELECT RAISE(ABORT,'failed'); END""", ())
        self.process()
        self.process(False)
        ram = self.app.jobs.ram_results()
        self.assertEqual([r.text for r in ram], [EDIT])
        widget = self.app.window._cards["ram-" + ram[0].id]
        widget.clicked.emit(EDIT)
        clip = Mock()
        clip.mimeData.side_effect = lambda: clip.setMimeData.call_args.args[0]
        with patch("gui.QGuiApplication.clipboard", return_value=clip):
            self.app.window._on_copy()
        clip.setMimeData.assert_called_once()
        self.assertEqual(clip.mimeData().text(), EDIT)
        self.assertIn("Copied", self.app.window.result_label.text())

    def test_explicit_copy_changes_only_delivery(self):
        self.process()
        with patch("gui.QGuiApplication.clipboard") as clipboard:
            clip = clipboard.return_value
            clip.mimeData.side_effect = lambda: clip.setMimeData.call_args.args[0]
            self.app.window._on_copy()
        row = self.app.history.page()[0]
        self.assertEqual((row["original"], row["text"], row["processing"], row["storage"], row["delivery"]),
                         (RAW, EDIT, "ready", "result", "copied"))

    def legacy_store(self):
        from history import HistoryStore
        store = HistoryStore(self.database.with_suffix(".legacy"), self.root)
        self.addCleanup(store.close)
        return store

    def test_migration_duplicates_bad_records_preferences_and_changed_snapshot(self):
        path = self.root / "history.json"
        repeated = {"id": "same", "text": RAW, "ts": 123456789.0}
        items = [repeated, repeated, {"text": EDIT, "ts": "unknown"}, None, {}, {"text": " "},
                 {"text": "\ud800"}]
        data = json.dumps(items).encode()
        path.write_bytes(data)
        state = self.root / "state.json"
        state_bytes = json.dumps({"audio_device": None, "lm_studio": True, "lm_model": 2,
                                  "autostart": True, "private": RAW}).encode()
        state.write_bytes(state_bytes)
        store = self.legacy_store()
        entries = store.page()
        self.assertEqual([e["text"] for e in entries], [RAW, RAW, EDIT])
        self.assertEqual(len({e["id"] for e in entries}), 3)
        self.assertEqual([e["ts"] for e in entries], [123456789.0, 123456789.0, None])
        self.assertTrue(all(e["original"] is None for e in entries))
        self.assertEqual(store.preferences(), {"audio_device": None, "lm_studio": True})
        report = store.reports["history"]
        self.assertEqual((report.imported, report.rejected), (3, 4))
        store.close()
        again = self.legacy_store()
        self.assertEqual(again.page(), entries)
        self.assertEqual(again.reports["history"].existing, 3)
        self.assertEqual(again.reports["history"].rejected, 4)
        again.close()
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(state.read_bytes(), state_bytes)
        path.write_bytes(b"[]")
        changed = self.legacy_store()
        self.assertEqual(changed.reports["history"].status, "source_changed")
        self.assertEqual(changed.page(), entries)

    def test_failed_import_rolls_back_then_retries(self):
        store = self.legacy_store()
        store._execute("""CREATE TRIGGER rollback_import BEFORE INSERT ON legacy_imports
                          BEGIN SELECT RAISE(ABORT,'failed'); END""", ())
        store.close()
        path = self.root / "history.json"
        path.write_text(json.dumps([{"text": RAW}] * 3), encoding="utf-8")
        failed = self.legacy_store()
        self.assertEqual(failed.reports["history"].status, "import_failed")
        self.assertEqual(failed.page(), [])
        failed._execute("DROP TRIGGER rollback_import", ())
        failed.close()
        restored = self.legacy_store()
        self.assertEqual(restored.reports["history"].imported, 3)
        self.assertEqual(len(restored.page()), 3)

    def test_invalid_legacy_root_can_be_repaired(self):
        path = self.root / "history.json"
        for data, status in ((b"{", "invalid_json"), (b"{}", "invalid_root"), (b"\xff", "invalid_json")):
            path.write_bytes(data)
            store = self.legacy_store()
            self.assertEqual(store.reports["history"].status, status)
            self.assertEqual(path.read_bytes(), data)
            store.close()
        path.write_bytes(b"[]")
        self.assertEqual(self.legacy_store().reports["history"].status, "imported")

    def test_future_schema_and_corrupt_db_preserved(self):
        from history import HistoryStore, StorageError
        for version in (False, True):
            path = self.database.with_suffix(".future" if version else ".corrupt")
            if version:
                with closing(sqlite3.connect(path)) as db:
                    db.execute("PRAGMA user_version=99")
            else:
                path.write_bytes(b"invalid sqlite")
            before = path.read_bytes()
            store = HistoryStore(path, self.root)
            self.addCleanup(store.close)
            self.assertEqual(store.reports["database"].status, "unavailable")
            with self.assertRaises(StorageError):
                store.original("job", RAW, False)
            store.close()
            self.assertEqual(path.read_bytes(), before)

    def test_history_paging_keeps_all_entries(self):
        store = self.app.history
        for i in range(107):
            store.original(str(i), RAW, False)
        first = store.page()
        middle = store.page(first[-1]["cursor"])
        last = store.page(middle[-1]["cursor"])
        self.assertEqual([int(row["id"]) for row in first + middle + last], list(reversed(range(107))))
        self.app.window.refresh_history()
        self.app.window._load_page()
        self.app.window._load_page()
        self.assertEqual(len(self.app.window._cards), 107)

    def test_unclean_process_exit_recovers_without_dispatch(self):
        from history import HistoryStore
        self.app.history.close()
        script = """import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from history import HistoryStore
s=HistoryStore(Path(sys.argv[2]),Path(sys.argv[3]))
s.start('stt')
s.start('llm')
s.original('llm','synthetic recovery',True)
s._execute("UPDATE jobs SET delivery='dispatching' WHERE id='llm'",())
os._exit(23)
"""
        proc = subprocess.run([sys.executable, "-B", "-c", script, str(self.root / "src"),
                               str(self.database), str(self.root)], timeout=10)
        self.assertEqual(proc.returncode, 23)
        restored = HistoryStore(self.database, self.root)
        self.addCleanup(restored.close)
        self.assertEqual(self.committed_rows("SELECT id,processing,storage,delivery FROM jobs ORDER BY id"),
                         [("llm", "interrupted", "original", "unknown"),
                          ("stt", "interrupted", "none", "not_requested")])
        self.assertEqual(restored.page()[0]["text"], "synthetic recovery")
        self.send.assert_not_called()

    def blocked_backend(self, smart):
        entered, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        def backend(*args, **kwargs):
            entered.set()
            if not resume.wait(5):
                raise RuntimeError("test backend timeout")
            return response(choice(EDIT)) if smart else RAW
        self.app.postprocessor.enabled = smart
        if smart:
            self.http.side_effect = backend
        else:
            self.block_app(self.app.transcriber, "transcribe", side_effect=backend)
        self.block("sounddevice.InputStream")
        self.app._on_hotkey_press()
        import numpy as np
        self.app.recorder._callback(np.ones((16000, 1)), 16000, None, None)
        self.app._on_hotkey_release()
        self.assertTrue(entered.wait(3))
        return resume, self.app._worker

    def test_cancel_during_stt_retains_slot_and_ignores_late_text(self):
        resume, worker = self.blocked_backend(False)
        self.app._on_cancel()
        self.app._on_hotkey_press()
        self.assertFalse(self.app.recorder.is_recording)
        resume.set()
        worker.join(3)
        self.qt.processEvents()
        self.assertFalse(worker.is_alive())
        self.assertIsNone(self.app.jobs.active)
        self.assertEqual(self.app.history.page(), [])
        self.assertEqual(self.committed_rows("SELECT processing FROM jobs"), [("cancelled",)])
        self.http.assert_not_called()

    def test_cancel_during_llm_preserves_editor_and_original(self):
        resume, worker = self.blocked_backend(True)
        self.app._on_cancel()
        self.app.window.text_edit.setPlainText("manual synthetic edit")
        resume.set()
        worker.join(3)
        self.qt.processEvents()
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.app.window.text_edit.toPlainText(), "manual synthetic edit")
        self.assertEqual(self.committed_rows("SELECT count(*) FROM results"), [(0,)])
        row = self.app.history.page()[0]
        self.assertEqual((row["original"], row["processing"], row["storage"]), (RAW, "cancelled", "original"))
        self.send.assert_not_called()
        self.copy.assert_not_called()

    def test_shutdown_while_llm_pending(self):
        self.block_app(self.app.qt_app, "quit")
        resume, worker = self.blocked_backend(True)
        self.app._on_quit()
        self.app.qt_app.setProperty("really_quit", False)
        self.app._on_hotkey_press()
        resume.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(self.app.recorder.is_recording)
        self.assertEqual(self.committed_rows("SELECT processing,storage,delivery FROM jobs"), [("cancelled","original","skipped")])
        self.assertEqual(self.committed_rows("SELECT count(*) FROM results"), [(0,)])
        self.send.assert_not_called()

    def test_quit_drains_an_accepted_original_write(self):
        # Hold the actual DB owner inside its transaction and request Quit from Qt.
        entered, release = threading.Event(), threading.Event()
        store = self.app.history
        def pause():
            entered.set()
            return int(release.wait(4))
        store._call(lambda: store._connection().create_function("pause_write", 0, pause))
        store._execute("""CREATE TRIGGER wait_for_quit BEFORE INSERT ON artifacts
                          BEGIN SELECT pause_write(); END""", ())
        import numpy as np
        self.block("sounddevice.InputStream")
        self.block_app(self.app.qt_app, "quit")
        self.app._on_hotkey_press()
        self.app.recorder._callback(np.ones((16000, 1)), 16000, None, None)
        self.app._on_hotkey_release()
        self.assertTrue(entered.wait(3))
        def unblock():
            self.app._closing.wait(3)
            release.set()
        helper = threading.Thread(target=unblock)
        helper.start()
        try:
            self.app._on_quit()
        finally:
            release.set()
            helper.join(3)
            self.app.qt_app.setProperty("really_quit", False)
        self.assertEqual(self.committed_rows("SELECT original_text FROM artifacts"), [(RAW,)])
        self.assertEqual(self.committed_rows("SELECT processing,storage,delivery FROM jobs"),
                         [("ready", "original", "skipped")])
        self.send.assert_not_called()

    def test_late_result_after_removal_keeps_artifact_hidden(self):
        resume, worker = self.blocked_backend(True)
        entry = self.app.history.page()[0]
        self.app.history.remove(entry["id"])
        resume.set()
        worker.join(3)
        self.qt.processEvents()
        self.assertEqual(self.app.history.page(), [])
        self.assertEqual(self.committed_rows("SELECT count(*) FROM results"), [(0,)])
        self.assertEqual(self.app.jobs.ram_results(), ())
        self.assertEqual(self.app.window.text_edit.toPlainText(), "")

    def test_cancel_recording_and_retry(self):
        ctor = self.block("sounddevice.InputStream")
        self.app._on_hotkey_press()
        self.app._on_cancel()
        self.app._on_hotkey_release()
        ctor.return_value.close.assert_called_once()
        self.assertFalse(self.app.recorder.is_recording)
        self.app._on_hotkey_press()
        self.assertTrue(self.app.recorder.is_recording)
        self.app._on_hotkey_release()
        self.assertEqual(self.app.history.page(), [])

    def test_recording_limit_rejects_whole_capture(self):
        import numpy as np
        import sounddevice as sd
        self.block("sounddevice.InputStream")
        recorder = self.app.recorder
        recorder.max_frames = 8000
        self.app._on_hotkey_press()
        block = np.zeros((7000, 1), dtype=np.float32)
        recorder._callback(block, 7000, None, None)
        with self.assertRaises(sd.CallbackStop):
            recorder._callback(block, 7000, None, None)
        self.assertLessEqual(recorder._buffer.nbytes, 4 * recorder.max_frames)
        self.app._on_hotkey_release()
        self.assertIn("limit", self.app.window.result_label.text())
        self.assertIsNone(self.app.jobs.active)
        self.assertIsNone(recorder._buffer)
        self.http.assert_not_called()

    def test_missing_configured_device_falls_back_to_default(self):
        from audio_recorder import AudioRecorder
        with patch("audio_recorder.list_input_devices", return_value=[(3, "synthetic mic")]):
            self.assertIsNone(AudioRecorder(device="stale device").device)
            self.assertEqual(AudioRecorder(device="synthetic mic").device, 3)
            self.assertEqual(AudioRecorder(device=2).device, 2)
        import sounddevice as sd
        with patch("audio_recorder.list_input_devices", return_value=[]), \
             patch("sounddevice.query_devices", return_value={"index": 7}), \
             patch("sounddevice.check_input_settings", side_effect=sd.PortAudioError("format", -9997)):
            self.assertIsNone(AudioRecorder(device="other host API").device)
