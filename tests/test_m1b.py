"""M1b delivery tests. The Windows integration only addresses HWNDs created by its fixture."""
from contextlib import ExitStack, closing
from ctypes import wintypes as W
import ctypes as C
from dataclasses import replace
from pathlib import Path
import os
import sqlite3
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

from test_m0 import IsolatedApp, RAW, EDIT


class SafeDeliveryTests(IsolatedApp):
    def setUp(self):
        super().setUp()
        from win_target import Target
        from delivery_worker import PinnedTarget
        self.snapshot = Target(40, 41, 42, 43, 6, 11, 9, 1, 41)
        self.native = Mock()
        self.native.capture.return_value = self.snapshot
        self.native.foreground.return_value = self.snapshot.window
        self.native.sequence.return_value = self.snapshot.clipboard_sequence
        self.span = Mock()
        self.span.matches.return_value = True
        self.span.text.return_value = "alpha"
        self.span.contains_result.side_effect = lambda start, text: self.span.text() == text
        self.span.replace.side_effect = lambda text: setattr(self.span.text, "return_value", text)
        self.changes = Mock()
        self.changes.unchanged.return_value = True
        stack = self.enterContext(ExitStack())
        stack.enter_context(patch("delivery_worker.Changes", return_value=self.changes))
        stack.enter_context(patch("delivery_worker.RichRange", return_value=self.span))
        pinned = PinnedTarget(stack, self.native, self.snapshot)
        self.attempt = Mock(verify=pinned.valid, deliver=Mock(side_effect=pinned.deliver))
        self.app.jobs.delivery = Mock()
        self.app.jobs.delivery.capture.return_value = self.attempt
        # Select through the user-facing entry point and capture at physical PTT admission.
        self.app.window.delivery_combo.setCurrentIndex(1)
        self.block("sounddevice.InputStream")

    def record(self, smart=False, during=None):
        self.app.postprocessor.enabled = smart
        self.app._on_hotkey_press()
        self.assertTrue(self.app.recorder.is_recording)
        self.app.recorder._callback(self.audio.reshape(-1, 1), len(self.audio), None, None)
        if during:
            during()
        self.app._on_hotkey_release()
        worker = self.app._worker
        if worker:
            worker.join(5)
            self.assertFalse(worker.is_alive())
        self.qt.processEvents()
        return self.app.history.page()[0]

    def test_raw_and_smart_deliver_only_committed_text(self):
        def insert(text):
            with closing(sqlite3.connect(self.database)) as observer:
                row = observer.execute("""SELECT COALESCE(results.text,original_text),original_text,delivery
                    FROM jobs JOIN artifacts ON jobs.artifact_id=artifacts.id
                    LEFT JOIN results ON results.artifact_id=artifacts.id
                    WHERE jobs.id=?""",
                    (self.app.jobs.active.id,)).fetchone()
            self.assertEqual(row, (text, RAW, "dispatching"))
            self.span.text.return_value = text
        self.span.replace.side_effect = insert
        for smart, text in ((False, RAW), (True, EDIT)):
            self.span.text.return_value = "alpha"
            row = self.record(smart)
            self.assertEqual((row["delivery"], row["text"]), ("confirmed", text))
            self.assertIn("confirmed", self.app.window.result_label.text())
        self.assertEqual(self.attempt.close.call_count, 2)
        self.send.assert_not_called()

    def test_field_window_process_and_equal_text_at_other_offset(self):
        for update in (dict(field=44), dict(window=49), dict(pid=50), dict(process_start=2),
                       dict(field_thread=45), dict(start=0, end=5)):
            with self.subTest(update=update):
                self.native.capture.return_value = self.snapshot
                row = self.record(during=lambda: setattr(self.native.capture, "return_value",
                                                         replace(self.snapshot, **update)))
                self.assertEqual((row["delivery"], row["original"]), ("skipped", RAW))
        self.span.replace.assert_not_called()

    def test_focus_away_and_back_and_closed_window(self):
        row = self.record(during=lambda: setattr(self.changes.unchanged, "return_value", False))
        self.assertEqual(row["delivery"], "skipped")
        self.changes.unchanged.return_value = True
        row = self.record(during=lambda: setattr(self.native.capture, "return_value", None))
        self.assertEqual(row["delivery"], "skipped")
        self.span.replace.assert_not_called()

    def test_target_changes_after_durable_intent(self):
        pinned_send = self.attempt.deliver.side_effect
        def before_send(text):
            self.native.capture.return_value = replace(self.snapshot, field=99)
            return pinned_send(text)
        self.attempt.deliver.side_effect = before_send
        self.assertEqual(self.record()["delivery"], "skipped")
        self.span.replace.assert_not_called()

    def test_new_clipboard_copy_after_verify(self):
        original_send = self.attempt.deliver.side_effect
        def before_send(text):
            self.native.capture.return_value = replace(self.snapshot, clipboard_sequence=10)
            return original_send(text)
        self.attempt.deliver.side_effect = before_send
        with patch("gui.QGuiApplication.clipboard", side_effect=AssertionError("clipboard access")):
            self.assertEqual(self.record()["delivery"], "skipped")
        self.span.replace.assert_not_called()

    def test_clipboard_formats_survive_autopaste(self):
        from PySide6.QtCore import QMimeData
        for fmt in ("text/plain", "image/png", "text/uri-list"):
            clipboard = QMimeData()
            clipboard.setData(fmt, b"synthetic")
            before = bytes(clipboard.data(fmt))
            self.span.text.return_value = "alpha"
            with patch("gui.QGuiApplication.clipboard", side_effect=AssertionError("clipboard access")):
                self.assertEqual(self.record()["delivery"], "confirmed")
            self.assertEqual(bytes(clipboard.data(fmt)), before)
        self.read.assert_not_called()
        self.copy.assert_not_called()

    def test_manual_paths_survive_unavailable_recipient_and_clipboard(self):
        self.app.jobs.delivery.capture.return_value = None
        self.assertEqual(self.record()["delivery"], "skipped")
        clip = Mock()
        clip.mimeData.side_effect = lambda: clip.setMimeData.call_args.args[0]
        with patch("gui.QGuiApplication.clipboard", return_value=clip):
            clip.ownsClipboard.return_value = False
            self.app.window._on_copy()
            self.assertEqual(self.app.history.page()[0]["delivery"], "skipped")
            clip.ownsClipboard.return_value = True
            self.app.window._on_copy()
            self.assertEqual(self.app.history.page()[0]["delivery"], "copied")
        self.app.window.delivery_combo.setCurrentIndex(0)
        self.assertEqual(self.record()["delivery"], "skipped")
        self.app.jobs.delivery.capture.assert_called_once()

    def test_cancel_before_dispatch_keeps_saved_text_and_manual_copy(self):
        entered, resume = threading.Event(), threading.Event()
        def verify():
            entered.set()
            return resume.wait(5)
        self.attempt.verify = verify
        self.app._on_hotkey_press()
        self.app.recorder._callback(self.audio.reshape(-1, 1), len(self.audio), None, None)
        self.app._on_hotkey_release()
        worker = self.app._worker
        try:
            self.assertTrue(entered.wait(3))
            self.app._on_cancel()
        finally:
            resume.set()
            worker.join(5)
        self.qt.processEvents()
        self.attempt.deliver.assert_not_called()
        self.assertEqual(self.app.window.text_edit.toPlainText(), RAW)
        self.assertEqual(self.app.history.page()[0]["delivery"], "skipped")

    def test_rejected_intent_excludes_dispatch(self):
        self.app.history._execute("""CREATE TRIGGER no_delivery BEFORE UPDATE OF delivery ON jobs
                WHEN NEW.delivery='dispatching' BEGIN SELECT RAISE(ABORT,'failed'); END""", ())
        self.assertEqual(self.record()["delivery"], "skipped")
        self.attempt.deliver.assert_not_called()

    def test_cancel_requested_while_intent_commit_is_busy(self):
        entered = threading.Event()
        job = None
        original = self.app.history.begin_delivery
        def begin(job_id, storage):
            entered.set()
            self.assertTrue(job.cancel_requested.wait(3))
            original(job_id, storage)
        self.block_app(self.app.history, "begin_delivery", side_effect=begin)
        self.app._on_hotkey_press()
        job = self.app.jobs.active
        self.app.recorder._callback(self.audio.reshape(-1, 1), len(self.audio), None, None)
        self.app._on_hotkey_release()
        worker = self.app._worker
        self.assertTrue(entered.wait(3))
        self.app._on_cancel()
        worker.join(3)
        self.qt.processEvents()
        self.assertEqual(self.app.history.page()[0]["delivery"], "skipped")
        self.assertEqual(self.app.window.text_edit.toPlainText(), RAW)
        self.attempt.deliver.assert_not_called()

    def test_receipt_commit_failure_is_unknown_and_never_retried(self):
        from history import HistoryStore
        self.app.history._execute("""CREATE TRIGGER no_receipt BEFORE UPDATE OF delivery ON jobs
                WHEN NEW.delivery='confirmed' BEGIN SELECT RAISE(ABORT,'failed'); END""", ())
        self.assertEqual(self.record()["delivery"], "dispatching")
        self.assertIn("Delivery unknown", self.app.window.result_label.text())
        self.app.history.close()
        restored = HistoryStore(self.database, self.root)
        self.addCleanup(restored.close)
        row = restored.page()[0]
        restored.copied(row["id"])
        self.assertEqual(restored.page()[0]["delivery"], "unknown")
        self.span.replace.assert_called_once_with(RAW)

    def test_removed_artifact_excludes_dispatch(self):
        def remove():
            row = self.app.history.page()[0]
            self.app.history.remove(row["id"])
            return True
        self.attempt.verify = remove
        self.app._on_hotkey_press()
        self.app.recorder._callback(self.audio.reshape(-1, 1), len(self.audio), None, None)
        self.app._on_hotkey_release()
        worker = self.app._worker
        if worker:
            worker.join(3)
        self.assertEqual(self.app.history.page(), [])
        self.attempt.deliver.assert_not_called()

    def test_copy_contention_does_not_report_old_owned_data_as_new_copy(self):
        self.app.jobs.delivery.capture.return_value = None
        self.record()
        clip = Mock()
        clip.ownsClipboard.return_value = True
        with patch("gui.QGuiApplication.clipboard", return_value=clip):
            self.app.window._on_copy()
        self.assertIn("could not be verified", self.app.window.result_label.text())
        self.assertEqual(self.app.history.page()[0]["delivery"], "skipped")
        self.assertEqual(self.app.window.text_edit.toPlainText(), RAW)

    def test_copy_during_addressed_write_is_preserved(self):
        from PySide6.QtGui import QGuiApplication
        self.assertEqual(QGuiApplication.platformName(), "offscreen")
        clip = QGuiApplication.clipboard()  # Private QPA backend only.
        clip.setText("first synthetic marker")
        def insert(text):
            clip.setText("new synthetic marker")
            self.span.text.return_value = text
        self.span.replace.side_effect = insert
        self.assertEqual(self.record()["delivery"], "confirmed")
        self.assertEqual(clip.text(), "new synthetic marker")

    def test_response_loss_and_dispatch_without_receipt_survive_restart(self):
        from history import HistoryStore
        self.attempt.deliver.side_effect = OSError("synthetic ambiguous outcome")
        self.assertEqual(self.record()["delivery"], "unknown")
        self.attempt.deliver.side_effect = None
        self.attempt.deliver.return_value = "paste_dispatched"
        self.assertEqual(self.record()["delivery"], "paste_dispatched")
        self.app.history.close()
        restored = HistoryStore(self.database, self.root)
        self.addCleanup(restored.close)
        self.assertEqual([row["delivery"] for row in restored.page()], ["unknown"] * 2)
        self.assertEqual(self.attempt.deliver.call_count, 2)

    def test_fallback_and_unsaved_smart_result_are_manual(self):
        import requests
        self.http.side_effect = requests.Timeout()
        self.assertEqual(self.record(True)["delivery"], "skipped")
        self.http.side_effect = None
        self.app.history._execute("""CREATE TRIGGER no_result BEFORE INSERT ON results
                BEGIN SELECT RAISE(ABORT,'failed'); END""", ())
        row = self.record(True)
        self.assertEqual((row["original"], row["delivery"]), (RAW, "skipped"))
        self.assertEqual(self.app.window.text_edit.toPlainText(), EDIT)
        self.attempt.deliver.assert_not_called()

    def test_no_confirmation_without_matching_readback(self):
        self.span.replace.side_effect = None
        self.assertEqual(self.record()["delivery"], "paste_dispatched")
        self.span.replace.assert_called_once_with(RAW)

    def test_no_change_on_nonplain_payload(self):
        self.block_app(self.app.transcriber, "transcribe", return_value="line\nnext")
        self.assertEqual(self.record()["delivery"], "skipped")
        self.span.replace.assert_not_called()


@unittest.skipUnless(sys.platform == "win32", "Windows Rich Edit")
class WindowsFieldTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = str(Path(__file__).resolve().parents[1] / "src")
        sys.path.insert(0, source)
        cls.addClassCleanup(sys.path.remove, source)

    def test_real_cross_process_range_receipt_and_equal_selections(self):
        self.check_native_provider([])

    def test_installed_notepad_provider_with_equal_selections(self):
        from win_target import document_profile
        # Resolve the installed package through its manifest registration. Never start Notepad.
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                                 "(Get-AppxPackage Microsoft.WindowsNotepad).InstallLocation"],
                                capture_output=True, text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        directory = Path(result.stdout.strip()) / "Notepad"
        if not document_profile(directory / "Notepad.exe", "Notepad", "RichEditD2DPT"):
            self.skipTest("Packaged Notepad unavailable")
        self.check_native_provider(["--notepad-provider", str(directory / "riched20.dll")])

    def test_d2d_plain_text_control_from_desktop_provider(self):
        library = Path(os.environ["ProgramFiles"]) / (
            "Microsoft Office/root/vfs/ProgramFilesCommonX64/Microsoft Shared/OFFICE16/RICHED20.DLL")
        if not library.is_file():
            self.skipTest("Desktop RichEditD2DPT provider unavailable")
        self.check_native_provider(["--notepad-provider", str(library)])

    def test_crash_after_real_field_write_recovers_unknown_without_replay(self):
        from tempfile import TemporaryDirectory
        from history import HistoryStore
        from win_target import WindowsTarget
        script = """import sys,os
from pathlib import Path
from contextlib import ExitStack
from types import SimpleNamespace
sys.path.insert(0,sys.argv[1])
from history import HistoryStore
from rich_edit import RichRange
from dictation import Coordinator
profile=Path(sys.argv[2])
store=HistoryStore(profile/'test.sqlite',profile)
with ExitStack() as stack:
    span=RichRange(stack,int(sys.argv[3]))
    class Attempt:
        def verify(self):
            return span.matches(0,0)
        def deliver(self,text):
            span.replace(text)
            os._exit(23)
    jobs=Coordinator(store,lambda outcome: None,SimpleNamespace(capture=Attempt))
    job=jobs.admit(SimpleNamespace(enabled=False),auto=True)
    jobs.run(job,None,lambda audio: 'saved synthetic')
"""
        with TemporaryDirectory() as directory:
            host = subprocess.Popen([sys.executable, "-B", str(Path(__file__).with_name("rich_edit_host.py"))],
                                    stdout=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                field, _ = map(int, host.stdout.readline().split())
                # Explicitly set the caret of our hidden synthetic receiver.
                native = WindowsTarget()
                result = C.c_size_t()
                self.assertTrue(native.send(field, 0xb1, 0, 0, 0x22, 500, C.byref(result)))
                child = subprocess.run([sys.executable, "-B", "-c", script,
                                        str(Path(__file__).resolve().parents[1] / "src"), directory, str(field)],
                                       timeout=8, creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(child.returncode, 23)
                for _ in range(2):
                    restored = HistoryStore(Path(directory) / "test.sqlite", Path(directory))
                    try:
                        row, = restored.page()
                        self.assertEqual((row["original"], row["delivery"]), ("saved synthetic", "unknown"))
                        value = C.create_unicode_buffer(80)
                        self.assertTrue(native.send(field, 0x0d, len(value), C.addressof(value), 0x22,
                                                    500, C.byref(result)))
                        self.assertEqual(value.value, "saved syntheticalpha alpha")
                    finally:
                        restored.close()
            finally:
                host.kill()
                host.wait(timeout=3)
                host.stdout.close()

    def check_native_provider(self, options):
        from rich_edit import RichRange
        from win_target import WindowsTarget
        host = subprocess.Popen([sys.executable, "-B", str(Path(__file__).with_name("rich_edit_host.py")), *options],
                                stdout=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            handles = host.stdout.readline().split()
            if not handles and options and host.wait(timeout=3) == 77:
                self.skipTest("Windows denied loading the packaged Notepad provider in a test host")
            first, second = map(int, handles)
            native = WindowsTarget()
            def select(hwnd, start, end):
                result = C.c_size_t()
                self.assertTrue(native.send(hwnd, 0xb1, start, end, 0x22, 500, C.byref(result)))
            select(first, 0, 5)
            select(second, 0, 5)
            with ExitStack() as stack:
                span = RichRange(stack, first)
                other = RichRange(stack, second)
                self.assertTrue(span.matches(0, 5))
                self.assertEqual(span.text(), "alpha")
                select(first, 6, 11)
                self.assertFalse(span.matches(0, 5))
                second_word = RichRange(stack, first)
                self.assertTrue(second_word.matches(6, 11))
                self.assertEqual(second_word.text(), "alpha")
                second_word.replace("Проба 🎙")
                self.assertEqual(second_word.text(), "Проба 🎙")
                self.assertEqual(span.text(), "alpha")
                self.assertEqual(other.text(), "alpha")
        finally:
            host.kill()
            host.wait(timeout=3)
            host.stdout.close()

    def test_manual_copy_on_private_windows_clipboard(self):
        result = subprocess.run([sys.executable, "-B", str(Path(__file__).with_name("private_clipboard.py"))],
                                timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode == 77:
            self.skipTest("Windows denied creation of a private window station")
        self.assertEqual(result.returncode, 0)

    def test_worker_protocol_and_windows_events_on_private_desktop(self):
        from delivery import Attempt
        from uuid import uuid4
        command = [sys.executable, "-B", str(Path(__file__).with_name("rich_edit_host.py")),
                   "--private-desktop", "whisper-test-" + uuid4().hex]
        host = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            window, pid, thread, field, _ = map(int, host.stdout.readline().split())
            for change_focus in (True, False):
                attempt = Attempt(subprocess.Popen([*command, "--test-worker", str(window), str(pid)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW))
                try:
                    snapshot = dict(pid=pid, thread=thread, window=window, field=field,
                                    start=0, end=0, clipboard_sequence=1, process_start=1, field_thread=thread)
                    self.assertEqual(attempt.exchange({"target": snapshot, "input_tick": 8}), "ready")
                    self.assertTrue(attempt.verify())
                    if change_focus:
                        host.stdin.write(b"move_focus_back\n")
                        host.stdin.flush()
                        self.assertEqual(host.stdout.readline().strip(), b"done")
                    expected = "skipped" if change_focus else "confirmed"
                    self.assertEqual(attempt.deliver("Проверка"), expected)
                finally:
                    attempt.close()
        finally:
            host.kill()
            host.wait(timeout=3)
            host.stdin.close()
            host.stdout.close()

    def test_process_policy_rejects_untrusted_paths_and_contexts(self):
        from win_target import document_profile
        root = os.environ["ProgramFiles"]
        wordpad = Path(root) / "Windows NT/Accessories/wordpad.exe"
        self.assertTrue(document_profile(wordpad, "WordPadClass", "RICHEDIT50W"))
        for image, window, field in (
                (Path("other/wordpad.exe"), "WordPadClass", "RICHEDIT50W"),
                (wordpad, "Dialog", "RICHEDIT50W"), (wordpad, "WordPadClass", "Edit"),
                (Path(root) / "terminal.exe", "WordPadClass", "RICHEDIT50W")):
            self.assertFalse(document_profile(image, window, field))
        from config import load_config
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            for value in ('"notepad.exe"', '["*"]', '[null]'):
                path.write_text("delivery:\n  excluded_processes: " + value, encoding="utf-8")
                self.assertIsNone(load_config(path).excluded_processes)
            path.write_text('delivery:\n  excluded_processes: ["NotePad.EXE"]', encoding="utf-8")
            self.assertEqual(load_config(path).excluded_processes, ("notepad.exe",))

    def test_protected_excluded_and_unavailable_fields_skip_before_reading_selection(self):
        from win_target import WindowsTarget
        native = WindowsTarget()
        native.foreground = lambda: 42
        def thread(hwnd, pid):
            pid._obj.value = 7
            return 8
        def info(tid, data):
            data._obj.active, data._obj.focus = 42, 43
            return True
        native.thread, native.gui = thread, info
        native.visible = native.enabled = lambda hwnd: True
        native.root = lambda hwnd, flags: 42
        native._class = lambda hwnd: "WordPadClass" if hwnd == 42 else "RICHEDIT50W"
        native._process = lambda pid: (str(Path(os.environ["ProgramFiles"]) / "Windows NT/Accessories/wordpad.exe"), 1)
        native.send = Mock()
        for style in (0, 1, 4 | 0x20, 4 | 0x800):
            native.style = lambda *args: style
            self.assertIsNone(native.capture())
        native.style = lambda *args: 4
        self.assertIsNone(native.capture(("wordpad.exe",)))
        native.send.assert_not_called()
        def password(hwnd, msg, wparam, lparam, flags, timeout, result):
            self.assertEqual(msg, 0xd2)
            result._obj.value = 42
            return True
        native.send = password
        self.assertIsNone(native.capture())

    def test_notepad_document_on_its_own_ui_thread(self):
        from win_target import WindowsTarget
        native = WindowsTarget()
        root, field, pid = 42, 43, 7
        root_thread, field_thread = 8, 12
        queues = {root_thread: (root, field, 0), field_thread: (root, field, 1)}
        owners = {root: (root_thread, pid), field: (field_thread, pid)}

        def thread(hwnd, out_pid):
            tid, process_id = owners[hwnd]
            out_pid._obj.value = process_id
            return tid

        def gui(tid, data):
            if tid not in queues:
                return False
            data._obj.active, data._obj.focus, data._obj.flags = queues[tid]
            return True

        def query(hwnd, message, wparam, lparam, flags, timeout, result):
            self.assertEqual(hwnd, field)
            if message == 0xb0:  # EM_GETSEL, both offsets of the empty caret
                C.c_uint32.from_address(wparam).value = 0
                C.c_uint32.from_address(lparam).value = 0
            else:
                self.assertEqual(message, 0xd2)
                result._obj.value = 0
            return True

        image = Path(os.environ["ProgramFiles"]) / (
            "WindowsApps/Microsoft.WindowsNotepad_11.2609.11.0_x64__8wekyb3d8bbwe/Notepad/Notepad.exe")
        native.foreground = lambda: root
        native.thread, native.gui = thread, gui
        native.root = lambda hwnd, flags: root
        native.visible = native.enabled = lambda hwnd: True
        native._class = lambda hwnd: "Notepad" if hwnd == root else "RichEditD2DPT"
        native._process = lambda candidate: (str(image), 1)
        native.style = lambda hwnd, index: 4
        native.sequence = lambda: 9
        native.send = Mock(side_effect=query)
        snapshot = native.capture()
        self.assertIsNotNone(snapshot)
        self.assertEqual((snapshot.thread, snapshot.field_thread, snapshot.field), (root_thread, field_thread, field))

        for state in ((root, field + 1, 0), (root + 1, field, 0), (root, field, 4), None):
            with self.subTest(field_thread_state=state):
                if state is None:
                    del queues[field_thread]
                else:
                    queues[field_thread] = state
                native.send.reset_mock()
                self.assertIsNone(native.capture())
                native.send.assert_not_called()
        queues[field_thread] = (root, field, 0)
        owners[field] = (field_thread, pid + 1)
        self.assertIsNone(native.capture())
        native.send.assert_not_called()
        owners[field] = (field_thread, pid)
        native._class = lambda hwnd: "WordPadClass" if hwnd == root else "RICHEDIT50W"
        native._process = lambda candidate: (str(Path(os.environ["ProgramFiles"]) /
                                                  "Windows NT/Accessories/wordpad.exe"), 1)
        self.assertIsNone(native.capture())
        native.send.assert_not_called()
