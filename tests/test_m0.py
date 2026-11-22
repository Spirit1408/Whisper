"""M0 regression suite. All stores and logs belong to a private temporary copy."""
import io
import json
import logging
import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

ROOT = Path(os.environ.get("M0_TEST_SOURCE", Path(__file__).resolve().parents[1]))
RAW = "Synthetic M0 marker 2937"
EDIT = RAW + "."


def response(payload, status=200):
    import requests
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload).encode()
    result._content_consumed = True
    result.url = "http://127.0.0.1:1234/v1/chat/completions"
    return result


def choice(content, reason="stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": reason}]}


class IsolatedApp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = TemporaryDirectory(prefix="whisper-m0-c-")
        cls.root = Path(cls.scratch.name)
        shutil.copytree(ROOT / "src", cls.root / "src", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copyfile(ROOT / "config.yaml", cls.root / "config.yaml")
        sys.path.insert(0, str(cls.root / "src"))
        cls.env = patch.dict(os.environ, {"QT_QPA_PLATFORM": "offscreen", "HF_HUB_OFFLINE": "1",
                             "HF_HOME": str(cls.root / "empty-cache"),
                             "HF_HUB_CACHE": str(cls.root / "empty-cache/hub"),
                             "HF_HUB_DISABLE_TELEMETRY": "1"})
        cls.env.start()
        from PySide6.QtWidgets import QApplication
        cls.qt = QApplication.instance() or QApplication([])
        import main
        cls.main = main

    @classmethod
    def tearDownClass(cls):
        for handler in logging.getLogger().handlers[:]:
            logging.getLogger().removeHandler(handler)
            handler.close()
        for name, module in list(sys.modules.items()):
            filename = getattr(module, "__file__", None)
            if filename and Path(filename).parent == cls.root / "src":
                del sys.modules[name]
        sys.path.remove(str(cls.root / "src"))
        cls.env.stop()
        cls.scratch.cleanup()

    def setUp(self):
        import numpy as np
        for filename in ("history.json", "state.json"):
            (self.root / filename).unlink(missing_ok=True)
        self.logs = io.StringIO()
        root = logging.getLogger()
        self.addCleanup(root.setLevel, root.level)
        root.setLevel(logging.DEBUG)
        self.handler = logging.StreamHandler(self.logs)
        logging.getLogger().addHandler(self.handler)
        self.addCleanup(logging.getLogger().removeHandler, self.handler)
        self.block("socket.socket.connect", side_effect=AssertionError("unexpected network"))
        self.block("socket.getaddrinfo", side_effect=AssertionError("unexpected DNS"))
        self.block("autostart.is_enabled", return_value=False)
        self.block("gui.list_input_devices", return_value=[])
        self.block("audio_recorder.list_input_devices", return_value=[])
        self.block("sounddevice.query_devices", return_value={"name": "test"})
        self.block("main.beep")
        self.send = self.block("keyboard.send")
        self.copy = self.block("pyperclip.copy")
        self.read = self.block("pyperclip.paste", return_value="Synthetic clipboard")
        self.http = self.block("requests.adapters.HTTPAdapter.send", return_value=response(choice(EDIT)))
        profile = TemporaryDirectory(dir=self.root)
        self.addCleanup(profile.cleanup)
        self.database = Path(profile.name) / "artifacts.sqlite3"
        self.app = self.main.DictationApp(self.qt, database=self.database, legacy_dir=self.root)
        self.addCleanup(self.app.history.close)
        self.notify = self.block_app(self.app.tray, "notify")
        self.app.config.sounds = False
        self.audio = np.full(16000, 0.1, dtype=np.float32)
        import transcriber
        from types import SimpleNamespace
        backend = Mock()
        backend.transcribe.return_value = ([SimpleNamespace(text=RAW)],
                                          SimpleNamespace(language="en", language_probability=1.0))
        stt = transcriber.Transcriber.__new__(transcriber.Transcriber)
        stt.model, stt.language, stt.sample_rate = backend, None, 16000
        self.app.transcriber = stt
        self.app.postprocessor.model = "synthetic"
        self.addCleanup(self.app.window.deleteLater)

    def block(self, target, **kw):
        obj = patch(target, **kw)
        self.addCleanup(obj.stop)
        return obj.start()

    def block_app(self, obj, key, **kw):
        handle = patch.object(obj, key, **kw)
        self.addCleanup(handle.stop)
        return handle.start()

    def process(self, smart=True):
        self.app.postprocessor.enabled = smart
        self.app._process(self.audio)
        self.qt.processEvents()

    def assert_result(self, expected, outcome):
        self.assertEqual([item["text"] for item in self.app.history.page()], [expected])
        self.assertEqual(self.app.window.text_edit.toPlainText(), expected)
        self.assertIn(outcome, self.app.window.result_label.text())
        self.assertIn("Copy", self.app.window.result_label.text())
        self.send.assert_not_called()
        self.read.assert_not_called()
        self.copy.assert_not_called()

class SafetyTests(IsolatedApp):
    def test_raw(self):
        self.process(False)
        self.http.assert_not_called()
        self.assert_result(RAW, "Raw")

    def test_smart(self):
        self.process()
        self.http.assert_called_once()
        self.assert_result(EDIT, "Smart")

    def test_privacy(self):
        import requests
        self.process(False)
        self.process()
        self.http.side_effect = requests.ConnectionError(RAW)
        self.process()
        self.assertNotIn(RAW, self.logs.getvalue())
        self.assertNotIn("Synthetic clipboard", self.logs.getvalue())
        from postprocessor import SYSTEM_PROMPT
        self.assertNotIn(SYSTEM_PROMPT, self.logs.getvalue())

    def test_changed_targets_and_clipboard(self):
        from PySide6.QtWidgets import QWidget, QVBoxLayout, QLineEdit
        host = QWidget()
        layout = QVBoxLayout(host)
        first, second, password, other = (QLineEdit() for _ in range(4))
        for field in (first, second, password):
            layout.addWidget(field)
        password.setEchoMode(QLineEdit.Password)
        self.addCleanup(host.close)
        self.addCleanup(other.close)
        host.show()
        other.show()
        for field in (first, other, second, password):
            field.window().activateWindow()
            field.setFocus()
            self.qt.processEvents()
            self.assertIs(self.qt.focusWidget(), field)
            self.process(False)
            self.send.assert_not_called()
            self.copy.assert_not_called()
            self.read.assert_not_called()

    def test_broken_paste_transport(self):
        self.copy.side_effect = OSError(RAW)
        self.send.side_effect = OSError(RAW)
        self.process(False)
        self.assert_result(RAW, "Raw")

    def test_unwritable_history(self):
        from history import HistoryStore
        self.app.history.close()
        store = HistoryStore(self.root, self.root)  # a directory cannot be opened as SQLite
        self.addCleanup(store.close)
        self.app.jobs.store = store
        self.process(False)
        self.assertEqual(self.app.window.text_edit.toPlainText(), RAW)
        self.assertIn("not saved", self.app.window.result_label.text())
        self.assertEqual(self.app.jobs.ram_results()[0].text, RAW)
        self.send.assert_not_called()

    def test_disk_full_preserves_previous_json(self):
        # Legacy files remain byte-identical; a failed result commit keeps both Originals.
        legacy = self.root / "history.json"
        legacy.write_bytes(b"[]")
        self.process(False)
        store = self.app.history
        store._execute("""CREATE TRIGGER full_disk BEFORE INSERT ON results
                          BEGIN SELECT RAISE(ABORT, 'write_failed'); END""", ())
        self.process()
        self.assertEqual(legacy.read_bytes(), b"[]")
        self.assertEqual(self.app.window.text_edit.toPlainText(), EDIT)
        self.assertIn("not saved", self.app.window.result_label.text())
        self.assertEqual([e["text"] for e in store.page()], [RAW, RAW])
        self.assertEqual(store.page()[0]["storage"], "original")

    def test_unreadable_settings_preserved(self):
        from history import HistoryStore
        from settings import SettingsStore
        path = self.root / "state.json"
        path.write_bytes(b"{")
        store = HistoryStore(self.database.with_suffix(".test"), self.root)
        self.addCleanup(store.close)
        SettingsStore(store).set("lm_model", "synthetic")
        self.assertEqual(store.reports["state"].status, "invalid_json")
        self.assertEqual(path.read_bytes(), b"{")

    def test_no_result_for_silence(self):
        self.audio.fill(0)
        self.process()
        self.assertFalse(self.app.history.page())
        self.http.assert_not_called()
        self.send.assert_not_called()

    def test_short_then_busy(self):
        self.block("sounddevice.InputStream")
        self.app._on_hotkey_press()
        self.app._on_hotkey_release()
        self.assertFalse(self.app.recorder.is_recording)
        self.assertIsNone(self.app.jobs.active)
        job = self.app.jobs.admit(self.app.postprocessor)
        self.app._on_hotkey_press()
        self.assertFalse(self.app.recorder.is_recording)
        self.app.jobs.release(job)
        self.http.assert_not_called()

    def test_quit_during_recording(self):
        stream = self.block("sounddevice.InputStream").return_value
        self.block_app(self.app.qt_app, "quit")
        self.app._on_hotkey_press()
        self.app._on_quit()
        stream.close.assert_called_once()
        self.assertFalse(self.app.recorder.is_recording)
        self.app._on_hotkey_release()
        self.http.assert_not_called()
        self.app.qt_app.setProperty("really_quit", False)

    def test_local_model_policy(self):
        from lmstudio import LMStudioManager
        lm = LMStudioManager()
        trace = []
        def run(args, timeout):
            from subprocess import CompletedProcess
            trace.append(args)
            if args[0] == "ls":
                return CompletedProcess([], 0, json.dumps([{"type":"llm","modelKey":"target"}]), "")
            if args[0] == "ps":
                return CompletedProcess([], 0, json.dumps([{"type":"llm","modelKey":"shared"}]), "")
            return CompletedProcess([], 0, "", "")
        self.block_app(lm, "_run", side_effect=run)
        self.assertFalse(lm.load_model("unknown"))
        self.assertTrue(lm.load_model("target"))
        self.assertTrue(lm.start_server())
        self.assertIn(["server", "start", "--bind", "127.0.0.1", "--port", "1234"], trace)
        self.assertFalse(any(cmd[0] == "unload" for cmd in trace))
        duplicate = {"type": "llm", "modelKey": "target"}
        with patch.object(lm, "_run_json", side_effect=[[duplicate], [duplicate, duplicate]]):
            before = len(trace)
            self.assertFalse(lm.load_model("target"))
            self.assertEqual(len(trace), before)

    def test_offline_stt(self):
        import transcriber
        backend = self.block("transcriber.WhisperModel")
        with self.assertRaises((FileNotFoundError, OSError, ValueError)):
            transcriber.Transcriber("large-v3-turbo")
        backend.assert_not_called()
        with self.assertRaises((FileNotFoundError, OSError, ValueError)):
            transcriber.Transcriber("missing-offline-model")
        backend.assert_not_called()
        incomplete = self.root / "incomplete"
        incomplete.mkdir(exist_ok=True)
        with self.assertRaises((FileNotFoundError, OSError, ValueError)):
            transcriber.Transcriber(str(incomplete))
        backend.assert_not_called()

    def test_external_endpoint(self):
        from postprocessor import PostProcessor
        self.app.postprocessor = PostProcessor(base_url="http://external.invalid/v1", model="probe")
        self.process()
        self.http.assert_not_called()
        self.assert_result(RAW, "fallback")

    def test_redirect(self):
        redirect = response({}, 307)
        redirect.headers["Location"] = "http://external.invalid/v1"
        def reply(prepared, **kwargs):
            result = redirect if self.http.call_count == 1 else response(choice(EDIT))
            result.request = prepared
            return result
        self.http.side_effect = reply
        self.process()
        self.http.assert_called_once()
        self.assert_result(RAW, "fallback")

    def test_proxy(self):
        with patch.dict(os.environ, {"HTTP_PROXY":"http://external.invalid:8888","NO_PROXY":""}):
            self.process()
        self.assertFalse(self.http.call_args.kwargs.get("proxies"))
        self.assert_result(EDIT, "Smart")

    def test_actual_log_destinations(self):
        from contextlib import redirect_stderr
        from postprocessor import SYSTEM_PROMPT
        import subprocess
        import requests
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.main.configure_logging()
            self.process(False)
            self.process()
            self.http.side_effect = requests.Timeout(RAW)
            self.process()
            self.block_app(self.app.lm, "lms_path", return_value="lms")
            self.block("lmstudio.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", EDIT))
            self.app.lm.start_server()
            logging.getLogger("faster_whisper").warning(SYSTEM_PROMPT)
            logging.getLogger("urllib3.connectionpool").warning(RAW)
        disk = (self.root / "app-technical.log").read_text(encoding="utf-8")
        for output in (disk, stderr.getvalue()):
            self.assertIn("smart_fallback", output)
            self.assertNotIn(RAW, output)
            self.assertNotIn(SYSTEM_PROMPT, output)
            self.assertNotIn("Synthetic clipboard", output)

    def inline_workers(self):
        def inline(*, target, **kwargs):
            return Mock(start=target)
        self.block("main.threading.Thread", side_effect=inline)

    def test_switch_with_shared_model(self):
        from subprocess import CompletedProcess
        self.inline_workers()
        self.block_app(self.app.lm, "lms_path", return_value="lms")
        for status in (0, 1, 137):
            with self.subTest(status=status):
                trace = []
                def run(args, timeout):
                    trace.append(args)
                    data = [{"type":"llm", "modelKey":"shared"}]
                    if args[0] == "ls" or (status == 0 and any(a[0] == "load" for a in trace)):
                        data.append({"type":"llm", "modelKey":"target", "identifier":"target-instance"})
                    return CompletedProcess([], status if args[0] == "load" else 0, json.dumps(data), RAW)
                with patch.object(self.app.lm, "_run", side_effect=run):
                    self.app._on_lm_model_changed("target")
                self.assertEqual(sum(a[0] == "load" for a in trace), 1)
                self.assertFalse(any(a[0] == "unload" for a in trace))
                self.assertEqual(self.app.window.lm_cb.isEnabled(), status == 0)
                self.assertEqual(self.app.postprocessor.model, "target-instance" if status == 0 else "")

    def test_online_without_selected_model(self):
        self.inline_workers()
        self.block_app(self.app.lm, "lms_path", return_value=None)
        self.block_app(self.app.lm, "is_server_up", return_value=True)
        self.block_app(self.app.lm, "loaded_llms", return_value={})
        self.app._ensure_lm_ready()
        self.assertFalse(self.app.window.lm_cb.isEnabled())
        self.assertFalse(self.app.postprocessor.enabled)

    def test_failed_explicit_copy(self):
        self.process(False)
        clipboard = Mock()
        clipboard.setText.side_effect = OSError(RAW)
        with patch("gui.QGuiApplication.clipboard", return_value=clipboard):
            self.app.window._on_copy()
        self.assertEqual(self.app.window.text_edit.toPlainText(), RAW)
        self.assertIn("Copy failed", self.app.window.result_label.text())

    def test_repeat_ptt_during_handoff(self):
        import numpy as np
        self.block("sounddevice.InputStream")
        self.app._on_hotkey_press()
        self.app.recorder._callback(np.ones((16000, 1)), 16000, None, None)
        with patch("main.threading.Thread") as thread:
            self.app._on_hotkey_release()
            self.app._on_hotkey_press()
            self.assertFalse(self.app.recorder.is_recording)
            job = thread.call_args.kwargs
            job["target"](*job["args"], **job.get("kwargs", {}))
        self.assertIsNone(self.app.jobs.active)
        self.assert_result(RAW, "Raw")


def fallback_test(payload=None, error=None, status=200, invalid_json=False):
    def run(self):
        if error:
            self.http.side_effect = error(RAW)
        else:
            result = response(payload, status)
            if invalid_json:
                result._content = b"{"
            self.http.return_value = result
        self.process()
        self.http.assert_called_once()
        self.assert_result(RAW, "fallback")
    return run


def microphone_test(stage):
    def run(self):
        ctor = self.block("sounddevice.InputStream")
        stream = ctor.return_value
        failing = ctor if stage == "open" else getattr(stream, stage)
        failing.side_effect = OSError(RAW)
        self.app._on_hotkey_press()
        if stage in ("stop", "close"):
            self.app._on_hotkey_release()
        self.assertFalse(self.app.recorder.is_recording)
        self.assertIsNone(self.app.jobs.active)
        self.assertNotIn("Listening", self.app.window.status_bar.currentMessage())
        if stage != "open":
            stream.close.assert_called_once()
        failing.side_effect = None
        self.app._on_hotkey_press()
        self.app._on_hotkey_press()
        self.assertTrue(self.app.recorder.is_recording)
        self.app._on_hotkey_release()
        self.assertFalse(self.app.recorder.is_recording)
        self.assertIsNone(self.app.jobs.active)
    return run


import requests
for name, packet in {
    "null": choice(None), "blank": choice("   "), "empty": choice(""),
    "length": choice("partial", "length"), "reason_missing": {"choices":[{"message":{"content":EDIT}}]},
    "no_choices": {}, "choices_empty": {"choices":[]}, "wrong_type": [],
    "message_missing": {"choices":[{}]}, "content_object": choice({}),
}.items():
    setattr(SafetyTests, "test_fallback_" + name, fallback_test(packet))
for name, kwargs in {"not_json":{"invalid_json":True}, "http":{"status":503},
                     "timeout":{"error":requests.Timeout}, "offline":{"error":requests.ConnectionError}}.items():
    setattr(SafetyTests, "test_fallback_" + name, fallback_test(**kwargs))
for stage in ("open", "start", "stop", "close"):
    setattr(SafetyTests, "test_microphone_" + stage, microphone_test(stage))


if __name__ == "__main__":
    unittest.main()
