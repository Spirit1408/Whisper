"""Smart dictation through PTT, real SQLite and Qt, with controlled STT/HTTP/recipient."""
import json
import threading
from unittest.mock import Mock, patch

import requests

from test_m0 import EDIT, RAW, IsolatedApp, choice, response


class SmartDictationTests(IsolatedApp):
    def setUp(self):
        super().setUp()
        self.store = self.app.history
        self.target = Mock()
        self.target.verify.return_value = True
        self.target.deliver.return_value = "confirmed"
        self.app.jobs.delivery = Mock()
        self.app.jobs.delivery.capture.return_value = self.target
        self.app.window.delivery_combo.setCurrentIndex(1)
        self.app.window.set_lm_ready(True, "synthetic")
        self.app.window.lm_cb.setChecked(True)
        self.block("sounddevice.InputStream")

    def release_audio(self):
        self.app._on_hotkey_press()
        self.assertTrue(self.app.recorder.is_recording)
        self.app.recorder._callback(self.audio.reshape(-1, 1), len(self.audio), None, None)
        self.app._on_hotkey_release()
        return self.app._worker

    def join(self, worker):
        if worker is not None:
            worker.join(5)
            self.assertFalse(worker.is_alive())
        self.qt.processEvents()

    def dictate(self):
        self.join(self.release_audio())
        return self.store.page()[0]

    def reopen(self):
        from history import HistoryStore
        self.store.close()
        restored = HistoryStore(self.database, self.root)
        self.addCleanup(restored.close)
        return restored.page()

    def test_ready_response_and_model_are_bound_to_the_admitted_original(self):
        def backend(prepared, **kwargs):
            packet = json.loads(prepared.body)
            row, = self.store.page()
            self.assertEqual(row["processing"], "transforming")
            self.assertEqual(packet["messages"][-1]["content"], f"<raw_text>{row['original']}</raw_text>")
            self.assertEqual(packet["model"], "synthetic")
            return response(choice(EDIT))

        self.http.side_effect = backend
        self.app._on_hotkey_press()
        self.app.window.lm_cb.setChecked(False)
        self.app.postprocessor.model = "next-request"
        self.app.recorder._callback(self.audio.reshape(-1, 1), len(self.audio), None, None)
        self.app._on_hotkey_release()
        self.join(self.app._worker)
        self.http.assert_called_once()
        self.target.deliver.assert_called_once_with(EDIT)
        row, = self.reopen()
        self.assertEqual((row["original"], row["text"], row["processing"], row["storage"], row["delivery"]),
                         (RAW, EDIT, "ready", "result", "confirmed"))

    def test_malformed_or_unfinished_response_never_reaches_recipient(self):
        packets = [choice(text) for text in ("\x00text", "\ud800", " \u200b ")]
        packets += [choice(EDIT, "tool_calls"), choice(EDIT, "content_filter"), choice(EDIT, "length"),
                    dict(choice(EDIT), error={"code": 1}), {"choices": choice(EDIT)["choices"] * 2}]
        for field, value in (("role", "user"), ("tool_calls", [{}]), ("function_call", {"name": "test"}),
                             ("refusal", "synthetic refusal")):
            packet = choice(EDIT)
            packet["choices"][0]["message"][field] = value
            packets.append(packet)
        for index, packet in enumerate(packets):
            with self.subTest(index=index):
                self.http.return_value = response(packet)
                row = self.dictate()
                code = "smart_truncated" if packet["choices"][0]["finish_reason"] == "length" else "smart_invalid"
                self.assertEqual((row["processing"], row["storage"], row["delivery"], row["error"]),
                                 ("fallback", "original", "skipped", code))
                self.assertIn("Smart fallback", self.app.window.result_label.text())
        for row in self.reopen():
            self.assertEqual((row["text"], row["original"]), (RAW, RAW))
        self.target.deliver.assert_not_called()

    def test_offline_and_timeout_preserve_previous_success_on_restart(self):
        previous = self.dictate()
        for error, code in ((requests.ConnectionError(), "smart_offline"), (requests.Timeout(), "smart_timeout")):
            self.http.side_effect = error
            row = self.dictate()
            self.assertEqual((row["text"], row["error"], row["delivery"]), (RAW, code, "skipped"))
            self.assertIn("Smart fallback", self.app.window.result_label.text())
        rows = self.reopen()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[-1], previous)
        self.target.deliver.assert_called_once_with(EDIT)

    def test_complete_but_overdue_response_is_manual_fallback(self):
        with patch("postprocessor.monotonic", side_effect=[0, self.app.postprocessor.timeout + 1]):
            row = self.dictate()
        self.assertEqual((row["error"], row["text"], row["storage"]), ("smart_timeout", RAW, "original"))
        self.target.deliver.assert_not_called()

    def test_saved_smart_choice_while_model_offline_requires_explicit_raw(self):
        self.app.window._set_lm_busy("checking...")
        with patch("main.threading.Thread", side_effect=lambda *, target, **kw: Mock(start=target)), \
                patch.object(self.app.lm, "is_server_up", return_value=False):
            self.app._probe_lm()
        self.assertTrue(self.app.window.lm_cb.isChecked())
        row = self.dictate()
        self.assertEqual((row["error"], row["delivery"]), ("smart_no_model", "skipped"))
        self.http.assert_not_called()
        self.target.deliver.assert_not_called()
        self.app.window.lm_cb.setChecked(False)
        row = self.dictate()
        self.assertEqual((row["processing"], row["delivery"]), ("ready", "confirmed"))
        self.target.deliver.assert_called_once_with(RAW)

    def test_original_read_failure_suppresses_llm_and_paste(self):
        from history import StorageError
        with patch.object(self.store, "smart_source", side_effect=StorageError()):
            row = self.dictate()
        self.http.assert_not_called()
        self.target.deliver.assert_not_called()
        self.assertEqual((row["original"], row["processing"], row["error"]), (RAW, "skipped", "original_read_failed"))

    def test_auto_request_with_failed_original_write_remains_manual_in_ram(self):
        self.store._execute("""CREATE TRIGGER reject_source BEFORE INSERT ON artifacts
                               BEGIN SELECT RAISE(ABORT, 'write failed'); END""", ())
        self.join(self.release_audio())
        self.http.assert_not_called()
        self.target.deliver.assert_not_called()
        will_copy, = self.app.jobs.ram_results()
        self.assertEqual((will_copy.text, will_copy.storage, will_copy.delivery), (RAW, "none", "skipped"))
        status = self.store._call(lambda: tuple(self.store._connection().execute(
            "SELECT processing,storage,delivery,error FROM jobs").fetchone()))
        self.assertEqual(status, ("skipped", "none", "skipped", "original_write_failed"))
        self.assertEqual(self.reopen(), [])

    def test_result_commit_rolls_back_and_keeps_prior_success_and_ram_candidate(self):
        from history import StaleAttempt
        previous = self.dictate()
        self.store._execute("CREATE TABLE guard (id TEXT REFERENCES results(artifact_id) DEFERRABLE INITIALLY DEFERRED)", ())
        self.store._execute("""CREATE TRIGGER reject_commit AFTER INSERT ON results
                               BEGIN INSERT INTO guard VALUES ('missing'); END""", ())
        row = self.dictate()
        self.assertEqual((row["text"], row["processing"], row["storage"], row["error"]),
                         (RAW, "ready", "original", "result_write_failed"))
        self.assertEqual(self.app.window.text_edit.toPlainText(), EDIT)
        self.assertEqual(self.app.jobs.ram_results()[0].text, EDIT)
        with self.assertRaises(StaleAttempt):
            self.store.begin_delivery(row["id"], "original")
        self.assertEqual(self.reopen(), [row, previous])
        self.target.deliver.assert_called_once_with(EDIT)

    def test_late_success_and_failure_cannot_relabel_same_completed_attempt(self):
        from history import StaleAttempt
        def first_completion(*args, **kw):
            self.store.result(self.app.jobs.active.id, "earlier completed correction")
            return self.http.return_value
        self.http.side_effect = first_completion
        for packet in (choice(EDIT), choice(None)):
            self.http.return_value = response(packet)
            row = self.dictate()
            self.assertEqual((row["text"], row["processing"], row["storage"], row["error"]),
                             ("earlier completed correction", "ready", "result", None))
            with self.assertRaises(StaleAttempt):
                self.store.finish(row["id"], "cancelled")
        self.assertEqual(len(self.reopen()), 2)
        self.assertEqual(self.app.window.text_edit.toPlainText(), "")
        self.target.deliver.assert_not_called()

    def test_late_fallback_after_trash_never_publishes_removed_original(self):
        def remove_during_http(*args, **kw):
            self.store.remove(self.app.jobs.active.id)
            raise requests.Timeout()
        self.http.side_effect = remove_during_http
        self.join(self.release_audio())
        self.assertEqual(self.reopen(), [])
        self.assertEqual(self.app.window.text_edit.toPlainText(), "")
        self.target.deliver.assert_not_called()

    def test_cancel_while_http_pending_then_new_request_delivers_only_once(self):
        entered, resume = threading.Event(), threading.Event()
        def delayed(*args, **kw):
            entered.set()
            if not resume.wait(4):
                raise RuntimeError("barrier timed out")
            return response(choice(EDIT))
        self.http.side_effect = delayed
        worker = self.release_audio()
        try:
            self.assertTrue(entered.wait(3))
            self.app._on_cancel()
        finally:
            resume.set()
            self.join(worker)
        self.target.deliver.assert_not_called()
        self.http.side_effect = None
        self.dictate()
        current, cancelled = self.reopen()
        self.assertEqual((cancelled["processing"], cancelled["text"], cancelled["delivery"]),
                         ("cancelled", RAW, "skipped"))
        self.assertEqual(current["delivery"], "confirmed")
        self.target.deliver.assert_called_once_with(EDIT)

    def test_changed_recipient_during_llm_keeps_both_texts_after_restart(self):
        def change(*args, **kw):
            self.target.verify.return_value = False
            return response(choice(EDIT))
        self.http.side_effect = change
        row = self.dictate()
        self.assertEqual((row["original"], row["text"], row["storage"], row["delivery"]),
                         (RAW, EDIT, "result", "skipped"))
        self.assertEqual(self.reopen(), [row])
        self.target.deliver.assert_not_called()

    def test_failed_fallback_status_write_recovers_as_interrupted(self):
        self.store._execute("""CREATE TRIGGER refuse_status BEFORE UPDATE OF processing ON jobs
                               WHEN NEW.processing='fallback' BEGIN SELECT RAISE(ABORT,'status_write'); END""", ())
        self.http.side_effect = requests.Timeout()
        row = self.dictate()
        self.assertEqual(row["processing"], "transforming")
        self.assertIn("processing status not saved", self.app.window.result_label.text())
        self.assertEqual(self.app.window.text_edit.toPlainText(), RAW)
        restored, = self.reopen()
        self.assertEqual((restored["text"], restored["processing"], restored["delivery"]),
                         (RAW, "interrupted", "skipped"))
        self.target.deliver.assert_not_called()
