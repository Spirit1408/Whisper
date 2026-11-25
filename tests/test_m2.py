"""M2 acceptance with private SQLite profiles and an isolated Qt/HTTP recipient."""
from contextlib import closing
from dataclasses import replace
import json
import sqlite3
import threading
from unittest.mock import Mock, patch

from test_m0 import EDIT, RAW, IsolatedApp, choice, response


class PresetTests(IsolatedApp):
    def setUp(self):
        super().setUp()
        self.store = self.app.history
        self.window = self.app.window
        self.controls = self.window.presets

    def select(self, key, once=False):
        combo = self.controls.once if once else self.controls.active
        index = combo.findData(key)
        self.assertGreaterEqual(index, 0)
        combo.setCurrentIndex(index)

    def join(self, worker):
        if worker is not None:
            worker.join(5)
            self.assertFalse(worker.is_alive())
        self.qt.processEvents()

    def repeat(self, artifact, source, dest, answer=EDIT):
        self.window._open_entry(artifact, source)
        self.select(dest, once=True)
        self.http.return_value = response(choice(answer))
        self.window._repeat()
        self.join(self.app._worker)
        return self.store.entry(artifact)

    def test_modes_share_transport_and_keep_independent_results(self):
        from presets import new_custom
        custom = replace(new_custom(), instruction="Return the input in brackets.")
        self.assertTrue(self.controls._save(custom, 0))
        self.process(False)
        row, = self.store.page()
        samples = {"strict": EDIT, "natural": EDIT, "professional": EDIT, "prompt": "Revised request",
                   "translator": "Перевод 2937", "formatter": "- " + RAW, "tasks": "- Check folder",
                   "summary": "Short summary", custom.id: "[" + RAW + "]", "raw": RAW}
        for key, value in samples.items():
            with self.subTest(key=key):
                row = self.repeat(row["id"], "original", key, value)
                self.assertEqual((row["original"], row["text"], row["delivery"]), (RAW, value, "not_requested"))
                self.assertEqual(self.controls.once.currentIndex(), 0)
        self.assertEqual(self.http.call_count, len(samples) - 1)
        self.assertEqual({k: v["text"] for k, v in self.store.versions(row["id"]).items()}, samples)
        self.assertEqual(self.store.versions(row["id"])["raw"]["model"], "")
        self.app.jobs.delivery = Mock()
        self.window.delivery_combo.setCurrentIndex(1)
        self.repeat(row["id"], "summary", "translator", "Краткий перевод")
        packet = json.loads(self.http.call_args.args[0].body)
        self.assertIn("Short summary", packet["messages"][1]["content"])
        version = self.store.versions(row["id"])["translator"]
        self.assertEqual((version["revision"], version["source_id"], version["source_revision"]), (2, "summary", 1))
        self.app.jobs.delivery.capture.assert_not_called()
        self.send.assert_not_called()
        self.copy.assert_not_called()
        self.read.assert_not_called()

    def test_ptt_freezes_definition_language_model_timeout_and_consumes_once(self):
        p = replace(self.window.selection.definitions["translator"], language="French", model="alternate", timeout=27)
        self.assertTrue(self.controls._save(p, 1))
        self.app._loaded_models = {"alternate": "instance-B"}
        self.select("natural")
        self.select("translator", once=True)
        self.block("sounddevice.InputStream")
        self.app._on_hotkey_press()
        self.select("summary", once=True)
        self.app._on_hotkey_press()  # Busy must keep the next selection
        self.assertEqual(self.window.selection.once, "summary")
        self.assertTrue(self.controls._save(replace(p, language="Italian", timeout=9), 2))
        self.app._loaded_models = {}
        self.app.postprocessor.model = "new-global"
        self.app.recorder._callback(self.audio.reshape(-1, 1), len(self.audio), None, None)
        self.app._on_hotkey_release()
        self.join(self.app._worker)
        packet = json.loads(self.http.call_args.args[0].body)
        self.assertEqual(packet["model"], "instance-B")
        self.assertIn("French", packet["messages"][0]["content"])
        self.assertEqual(self.http.call_args.kwargs["timeout"], 27)
        row, = self.store.page()
        self.assertEqual(self.store.versions(row["id"])["translator"]["preset_revision"], 2)
        self.app._process(self.audio)
        self.assertEqual(json.loads(self.http.call_args.args[0].body)["model"], "new-global")
        self.assertEqual(self.window.selection.active, "natural")
        self.assertEqual(self.store.preferences()["active_preset"], "natural")
        self.assertIsNone(self.window.selection.once)

    def test_specialized_failures_do_not_send_raw_or_modify_clipboard(self):
        self.window.delivery_combo.setCurrentIndex(1)
        target = Mock()
        self.app.jobs.delivery = Mock()
        self.app.jobs.delivery.capture.return_value = target
        self.block("sounddevice.InputStream")
        from presets import new_custom
        custom = replace(new_custom(), instruction="Return initials.")
        self.controls._save(custom, 0)
        for key in ["translator", "summary", "formatter", "tasks", "prompt", custom.id]:
            self.select(key)
            self.http.return_value = response(choice("partial", "length"))
            self.app._on_hotkey_press()
            self.app.recorder._callback(self.audio.reshape(-1, 1), len(self.audio), None, None)
            self.app._on_hotkey_release()
            self.join(self.app._worker)
            row = self.store.page()[0]
            self.assertEqual((row["text"], row["error"], row["processing"], row["delivery"]),
                             (RAW, "smart_truncated", "failed", "skipped"))
            self.assertEqual(self.store.versions(row["id"]), {})
        target.deliver.assert_not_called()
        self.copy.assert_not_called()
        self.send.assert_not_called()
        self.read.assert_not_called()

    def test_strict_semantic_corpus(self):
        from postprocessor import validate_candidate, SmartFailure
        from presets import BUILTINS
        corpus = [
            ("Анна, не переноси 17 папок. Они готовы?", "Анна, переноси 17 папок. Они готовы?"),
            ("Did Alice send 7 files?", "Did Alice send 8 files?"),
            ("Did Alice send 7 files?", "Did Alan send 7 files?"),
            ("Did Alice send 7 files?", "Alice sent 7 files."),
            ("Они готовы?", "Они готовы."),
            ("Balance is -3.", "Balance is 3."),
            ("Bob did not approve it.", "Bob approved it."),
        ]
        for source, bad in corpus:
            with self.subTest(source=source, bad=bad):
                with self.assertRaises(SmartFailure):
                    validate_candidate(BUILTINS["strict"], source, bad)
        source = "um Alice do not send seven files are they ready?"
        candidate = "Alice, do not send 7 files. Are they ready?"
        self.assertEqual(validate_candidate(BUILTINS["strict"], source, candidate), candidate)
        self.assertEqual(validate_candidate(BUILTINS["strict"], "ээ Анна не трогай 7 папок",
                                           "Анна, не трогай 7 папок."), "Анна, не трогай 7 папок.")
        self.select("strict")
        self.http.return_value = response(choice("Synthetic M0 marker 99."))
        self.app._process(self.audio)
        self.assertEqual(self.store.page()[0]["error"], "meaning_changed")

    def test_safe_json_import_export_and_revision(self):
        from presets import InvalidPreset, MAX_FILE, export_custom, import_custom, new_custom
        from history import StaleAttempt
        p = replace(new_custom(), name="Test", instruction="Return a list.", timeout=10, model="alternate")
        data = export_custom(p)
        new = import_custom(data)
        self.assertNotEqual(new.id, p.id)
        self.assertEqual(export_custom(new), data)
        packet = json.loads(data)
        malformed = [b"{", b"\xff", b" " * (MAX_FILE + 1), b'{"version":1,"version":2}',
                     b'{"version":true,"preset":{}}', json.dumps(dict(packet, version=2)).encode()]
        for key, value in (("base_url", "https://example.invalid"), ("delivery_mode", "auto"),
                           ("id", "raw"), ("timeout", True), ("timeout", 301), ("instruction", []),
                           ("model", "\x00"), ("name", "\udfff")):
            malformed.append(json.dumps(dict(packet, preset=dict(packet["preset"], **{key: value}))).encode())
        for bad in malformed:
            with self.assertRaises(InvalidPreset):
                import_custom(bad)
        # Exercise the file actions through the existing Qt window.
        path = self.database.with_suffix(".json")
        path.write_bytes(data)
        with patch("preset_ui.QFileDialog.getOpenFileName", return_value=(str(path), "")):
            self.controls.read_file()
        saved, = [v for v in self.store.presets().values() if v.custom]
        self.select(saved.id)
        dest = path.with_suffix(".export.json")
        with patch("preset_ui.QFileDialog.getSaveFileName", return_value=(str(dest), "")):
            self.controls.write_file()
            self.assertEqual(dest.read_bytes(), data)
            self.controls.write_file()
            self.assertIn("Export failed", self.window.result_label.text())
        self.store.save_preset(replace(saved, name="Renamed"), 1)
        with self.assertRaises(StaleAttempt):
            self.store.save_preset(saved, 1)
        with patch("preset_ui.QFileDialog.getOpenFileName", return_value=(str(path), "")):
            path.write_bytes(malformed[-1])
            self.controls.read_file()
        self.assertEqual(self.store.preferences(), {"active_preset": saved.id})
        self.assertEqual(len([v for v in self.store.presets().values() if v.custom]), 1)
        self.http.assert_not_called()

    def test_unavailable_override_never_uses_global_model(self):
        p = replace(self.window.selection.definitions["translator"], model="absent")
        self.controls._save(p, 1)
        self.select("translator")
        self.app._process(self.audio)
        row, = self.store.page()
        self.assertEqual((row["error"], row["processing"]), ("smart_no_model", "failed"))
        self.http.assert_not_called()

    def test_corrupt_definition_cannot_fall_back_to_raw_on_restart(self):
        self.select("translator")
        self.window.delivery_combo.setCurrentIndex(1)
        self.store._execute("INSERT INTO presets VALUES('translator','{}',2)", ())
        self.store.close()
        restarted = self.main.DictationApp(self.qt, database=self.database, legacy_dir=self.root)
        self.addCleanup(restarted.history.close)
        self.addCleanup(restarted.window.deleteLater)
        restarted.jobs.delivery = Mock()
        self.assertIsNone(restarted._admit(auto=True))
        restarted.jobs.delivery.capture.assert_not_called()
        self.assertEqual(restarted.window.selection.active, "translator")
        self.assertIn("raw", restarted.window.selection.definitions)

    def test_custom_instructions_and_responses_stay_out_of_logs(self):
        from presets import new_custom
        p = replace(new_custom(), instruction="Synthetic private instruction M2", name="Private M2 label")
        self.controls._save(p, 0)
        self.select(p.id)
        self.http.return_value = response(choice("Synthetic private response M2"))
        self.app._process(self.audio)
        self.http.return_value = response(choice(None))
        self.app._process(self.audio)
        for private in (p.name, p.instruction, RAW, "Synthetic private response M2"):
            self.assertNotIn(private, self.logs.getvalue())

    def test_specialized_delivery_observes_commit_and_current_policy(self):
        self.select("translator")
        self.window.delivery_combo.setCurrentIndex(1)
        self.block("sounddevice.InputStream")
        target = Mock()
        self.app.jobs.delivery = Mock()
        self.app.jobs.delivery.capture.return_value = target
        translation = "Синтетический маркер 2937"
        self.http.return_value = response(choice(translation))
        def receive(text):
            with closing(sqlite3.connect(self.database)) as db:
                row = db.execute("""SELECT r.preset_id,r.text,j.delivery FROM results r
                                    JOIN jobs j ON r.job_id=j.id WHERE j.id=?""",
                                 (self.app.jobs.active.id,)).fetchone()
                self.assertEqual(row, ("translator", text, "dispatching"))
            return "confirmed"
        target.deliver.side_effect = receive
        for revoke in (False, True):
            self.app._on_hotkey_press()
            if revoke:
                self.window.delivery_combo.setCurrentIndex(0)
                self.window.delivery_combo.setCurrentIndex(1)
            self.app.recorder._callback(self.audio.reshape(-1, 1), len(self.audio), None, None)
            self.app._on_hotkey_release()
            self.join(self.app._worker)
            self.assertEqual(self.store.page()[0]["delivery"], "skipped" if revoke else "confirmed")
        target.deliver.assert_called_once_with(translation)

    def test_specialized_echo_with_punctuation_stays_manual(self):
        self.select("translator")
        self.window.delivery_combo.setCurrentIndex(1)
        self.block("sounddevice.InputStream")
        self.app.jobs.delivery = Mock()
        self.http.return_value = response(choice(RAW.upper() + "!"))
        self.app._on_hotkey_press()
        self.app.recorder._callback(self.audio.reshape(-1, 1), len(self.audio), None, None)
        self.app._on_hotkey_release()
        self.join(self.app._worker)
        row, = self.store.page()
        self.assertEqual((row["storage"], row["delivery"]), ("result", "skipped"))
        self.assertIn("repeats source", self.window.result_label.text())
        self.app.jobs.delivery.capture.return_value.deliver.assert_not_called()

    def test_custom_editor_and_persistent_choice_survive_app_restart(self):
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication, QDialogButtonBox, QLineEdit, QPlainTextEdit
        def fill():
            dialog = QApplication.activeModalWidget()
            dialog.findChild(QLineEdit).setText("Named preset")
            dialog.findChild(QPlainTextEdit).setPlainText("Return one heading.")
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Save).click()
        QTimer.singleShot(0, fill)
        self.controls.create()
        custom, = [p for p in self.store.presets().values() if p.custom]
        self.assertEqual(custom.name, "Named preset")
        self.select(custom.id)
        self.select("raw", once=True)
        self.store.close()
        restarted = self.main.DictationApp(self.qt, database=self.database, legacy_dir=self.root)
        self.addCleanup(restarted.history.close)
        self.addCleanup(restarted.window.deleteLater)
        self.assertEqual(restarted.window.presets.active.currentData(), custom.id)
        self.assertIsNone(restarted.window.presets.once.currentData())
        self.assertTrue(restarted.postprocessor.enabled)
        self.assertEqual(restarted.history.presets()[custom.id], custom)

    def test_family_validation_and_trash_during_repeat(self):
        self.process(False)
        artifact = self.store.page()[0]["id"]
        for key, value in (("tasks", "an unstructured answer"), ("formatter", "changed quantity 27")):
            row = self.repeat(artifact, "original", key, value)
            self.assertEqual(row["processing"], "failed")
            self.assertEqual(row["text"], RAW)
        def remove(*args, **kwargs):
            self.store.remove(artifact)
            return response(choice("obsolete"))
        self.http.side_effect = remove
        self.window._open_entry(artifact, "original")
        self.select("summary", once=True)
        self.window._repeat()
        self.join(self.app._worker)
        self.assertEqual(self.store.page(), [])
        self.assertEqual(self.store.versions(artifact), {})

    def test_delete_last_entry_then_dictate_opens_only_the_new_versions(self):
        self.process()
        first = self.store.page()[0]["id"]
        self.repeat(first, "original", "translator", "Synthetic translation")
        self.assertEqual(self.window.version_combo.count(), 3)
        self.window.text_edit.clear()
        self.window._cards[first]._delete_btn.click()
        self.assertEqual(self.store.page(), [])
        combo = self.window.version_combo
        self.assertEqual(combo.count(), 0)
        self.assertFalse(combo.isEnabled())
        self.assertIsNone(self.window._artifact)
        self.assertFalse(self.window._dirty)

        self.select("natural")
        self.http.return_value = response(choice(EDIT))
        self.app._process(self.audio)
        self.qt.processEvents()
        second, = self.store.page()
        self.assertEqual(self.window._artifact, second["id"])
        self.assertTrue(combo.isEnabled())
        self.assertEqual([combo.itemData(i) for i in range(combo.count())], ["original", "natural"])
        self.assertEqual((combo.currentData(), self.window.text_edit.toPlainText()), ("natural", EDIT))
        combo.setCurrentIndex(0)
        self.assertEqual(self.window.text_edit.toPlainText(), RAW)
        self.window._cards[second["id"]].clicked.emit(second["text"])
        self.window.text_edit.setPlainText("Synthetic manual revision")
        self.window._save_edit()
        self.assertEqual(self.store.versions(second["id"])["natural"]["revision"], 2)

    def test_deletion_preserves_unrelated_selection_and_unsaved_text(self):
        self.process(False)
        first = self.store.page()[0]["id"]
        self.process()
        current = self.store.page()[0]["id"]
        self.window.text_edit.setPlainText("Synthetic draft")
        self.window._cards[first]._delete_btn.click()
        self.assertEqual(self.window._artifact, current)
        self.assertEqual(self.window.version_combo.currentData(), "strict")
        self.assertEqual(self.window.text_edit.toPlainText(), "Synthetic draft")
        self.window._cards[current]._delete_btn.click()
        self.assertEqual([r["id"] for r in self.store.page()], [current])
        self.assertEqual(self.window.text_edit.toPlainText(), "Synthetic draft")

        self.window._discard_edit()
        self.window._cards[current]._delete_btn.click()
        self.assertEqual(self.window.version_combo.count(), 0)
        self.assertEqual(self.window.text_edit.toPlainText(), "")

    def test_failed_delete_keeps_card_and_editor_bound(self):
        from history import StorageError
        self.process()
        current = self.store.page()[0]["id"]
        with patch.object(self.store, "remove", side_effect=StorageError()):
            self.window._cards[current]._delete_btn.click()
        self.assertIn(current, self.window._cards)
        self.assertEqual(self.window._artifact, current)
        self.assertEqual(self.window.version_combo.currentData(), "strict")
        self.assertEqual(self.window.text_edit.toPlainText(), EDIT)

    def test_discard_recovers_when_selected_entry_already_removed(self):
        self.process()
        current = self.store.page()[0]["id"]
        self.store.remove(current)
        self.window.text_edit.clear()
        self.window._discard_edit()
        self.assertEqual(self.window.version_combo.count(), 0)
        self.process(False)
        self.assertEqual(self.window.text_edit.toPlainText(), RAW)
        self.assertEqual(self.window.version_combo.count(), 1)

    def test_edit_repeat_failure_restart_and_prior_success(self):
        from history import HistoryStore
        self.process()
        row, = self.store.page()
        artifact = row["id"]
        self.window._open_entry(artifact, "original")
        self.window.text_edit.setPlainText("Human edit")
        self.window._save_edit()
        self.assertEqual(self.store.entry(artifact)["original"], RAW)
        self.assertEqual(self.store.versions(artifact)["raw"]["manual"], 1)
        self.repeat(artifact, "raw", "translator", "Перевод")
        self.window.text_edit.setPlainText("Правка")
        self.window._save_edit()
        before = self.store.versions(artifact)
        self.window._open_entry(artifact, "translator")
        self.select("translator", once=True)
        self.http.return_value = response(choice(None))
        self.window._repeat()
        self.join(self.app._worker)
        self.assertEqual(self.window.text_edit.toPlainText(), "Правка")
        self.assertEqual(self.store.entry(artifact)["processing"], "failed")
        self.store.close()
        restored = HistoryStore(self.database, self.root)
        self.addCleanup(restored.close)
        self.assertEqual(restored.versions(artifact), before)
        self.assertEqual(restored.entry(artifact)["original"], RAW)

    def test_edit_during_inference_preserves_draft_and_saved_revision(self):
        self.process()
        artifact = self.store.page()[0]["id"]
        wait, resume = threading.Event(), threading.Event()
        def delayed(*args, **kwargs):
            wait.set()
            self.assertTrue(resume.wait(4))
            return response(choice(EDIT))
        self.http.side_effect = delayed
        self.window._open_entry(artifact, "strict")
        self.select("strict", once=True)
        self.window._repeat()
        worker = self.app._worker
        try:
            self.assertTrue(wait.wait(3))
            self.window.text_edit.setPlainText("User correction")
            self.window._save_edit()
        finally:
            resume.set()
            self.join(worker)
        version = self.store.versions(artifact)["strict"]
        self.assertEqual((version["text"], version["revision"], version["manual"]), ("User correction", 2, 1))
        self.assertEqual(self.window.text_edit.toPlainText(), version["text"])
        self.assertEqual(self.store.entry(artifact)["original"], RAW)

    def test_stale_source_destination_and_superseded_attempt(self):
        from history import StaleAttempt
        from presets import BUILTINS
        self.process()
        artifact = self.store.page()[0]["id"]
        p = BUILTINS["translator"]
        self.store.start_repeat("old", artifact, "strict", 1, p, "A")
        self.store.start_repeat("new", artifact, "strict", 1, p, "B")
        with self.assertRaises(StaleAttempt):
            self.store.result("old", "stale candidate")
        self.store.result("new", "saved translation")
        self.store.start_repeat("pending", artifact, "strict", 1, p, "B")
        self.store.edit(artifact, "strict", 1, "edited source", "strict")
        with self.assertRaises(StaleAttempt):
            self.store.result("pending", "obsolete translation")
        with self.assertRaises(StaleAttempt):
            self.store.start_repeat("wrong", artifact, "strict", 1, p, "B")
        self.store.start_repeat("pending2", artifact, "original", 0, p, "B")
        self.store.edit(artifact, "translator", 1, "edited destination", "translator")
        with self.assertRaises(StaleAttempt):
            self.store.finish("pending2", "failed", "smart_failed")
        with self.assertRaises(StaleAttempt):
            self.store.result("pending2", "late")
        with self.assertRaises(StaleAttempt):
            self.store.edit(artifact, "translator", 1, "stale edit", "translator")
        self.assertEqual(self.store.versions(artifact)["translator"]["text"], "edited destination")

    def test_transaction_failure_on_repeat_preserves_saved_and_ram_versions(self):
        self.process()
        artifact = self.store.page()[0]["id"]
        self.repeat(artifact, "original", "translator", "first")
        previous = self.store.versions(artifact)
        self.store._execute("""CREATE TRIGGER fail_repeat BEFORE UPDATE ON results
                               BEGIN SELECT RAISE(ABORT,'failed'); END""", ())
        self.repeat(artifact, "original", "translator", "second")
        self.assertEqual(self.store.versions(artifact), previous)
        self.assertEqual(self.app.jobs.ram_results()[0].text, "second")
        self.assertEqual(self.window.text_edit.toPlainText(), "second")

    def test_migrate_v1_backup_resume_and_legacy_repeat(self):
        from history import HistoryStore, SCHEMA
        path = self.database.with_suffix(".old")
        with closing(sqlite3.connect(path)) as db, db:
            for ddl in SCHEMA:
                db.execute(ddl)
            db.execute("INSERT INTO artifacts(id,original_text) VALUES('a','old source')")
            db.execute("INSERT INTO artifacts(id,legacy_text) VALUES('b','known legacy')")
            db.execute("INSERT INTO results VALUES('a','old correction',3)")
            db.execute("INSERT INTO jobs VALUES('a','a','ready','result','confirmed',NULL)")
            db.execute("PRAGMA user_version=1")
        store = HistoryStore(path, self.root)
        self.addCleanup(store.close)
        self.assertEqual(store.versions("a")["strict"]["revision"], 3)
        self.assertEqual(store.entry("a")["delivery"], "confirmed")
        self.assertIsNone(store.entry("b")["original"])
        from presets import BUILTINS
        store.start_repeat("legacy-repeat", "b", "legacy", 0, BUILTINS["summary"], "model")
        self.assertEqual(store.smart_source("legacy-repeat"), "known legacy")
        store.result("legacy-repeat", "summary")
        store.close()
        again = HistoryStore(path, self.root)
        self.addCleanup(again.close)
        self.assertIsNone(again.entry("b")["original"])
        self.assertEqual(again.entry("b")["text"], "summary")
        backup, = path.parent.glob(path.name + ".v1-*")
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (1,))
            self.assertEqual(db.execute("SELECT * FROM results").fetchall(), [("a", "old correction", 3)])

    def test_migration_failure_restores_v1_and_retries(self):
        from history import HistoryStore, SCHEMA, migrate_results
        path = self.database.with_suffix(".retry")
        with closing(sqlite3.connect(path)) as db, db:
            for ddl in SCHEMA:
                db.execute(ddl)
            db.execute("INSERT INTO artifacts(id,original_text) VALUES('a','synthetic')")
            db.execute("INSERT INTO results VALUES('a','previous',2)")
            db.execute("PRAGMA user_version=1")
        def fail(db):
            migrate_results(db)
            raise sqlite3.OperationalError("synthetic failure")
        with patch("history.migrate_results", side_effect=fail):
            unavailable = HistoryStore(path, self.root)
            self.assertEqual(unavailable.reports["database"].status, "unavailable")
            unavailable.close()
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT * FROM results").fetchall(), [("a", "previous", 2)])
        store = HistoryStore(path, self.root)
        self.addCleanup(store.close)
        self.assertEqual(store.versions("a")["strict"]["text"], "previous")
