"""Entry point: wires together GUI, tray, hotkey, recorder, transcriber, post-processor."""

from __future__ import annotations

import logging
import sys
import tempfile
import threading
from pathlib import Path

from PySide6.QtCore import QLockFile
from PySide6.QtWidgets import QApplication, QMessageBox

from audio_recorder import AudioRecorder, resolve_device
from config import load_config
from gui import GuiBridge, MainWindow
from history import HistoryStore
from hotkey import PushToTalkHotkey
from lmstudio import LMStudioManager
from postprocessor import PostProcessor
from settings import SettingsStore
from text_inserter import TextInserter
from tray import TrayIcon

LOG_FILE = Path(__file__).resolve().parent.parent / "app.log"

# File logging is required for windowless runs via pythonw.exe (no stderr)
_handlers: list[logging.Handler] = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
if sys.stderr is not None:
    _handlers.append(logging.StreamHandler())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=_handlers,
)
logger = logging.getLogger("main")


def beep(start: bool) -> None:
    if sys.platform != "win32":
        return
    import winsound

    frequency = 880 if start else 440
    threading.Thread(
        target=winsound.Beep, args=(frequency, 120), daemon=True
    ).start()


class DictationApp:
    def __init__(self, qt_app: QApplication) -> None:
        self.qt_app = qt_app
        self.config = load_config()
        logger.info("Config loaded: hotkey=%s, model=%s", self.config.hotkey, self.config.model.name)

        self.settings = SettingsStore()
        self.history = HistoryStore()

        # GUI selection (state.json) wins over config.yaml; both fall back to default
        saved_device = self.settings.get("audio_device")
        device = resolve_device(saved_device) if saved_device else self.config.audio.device
        self.recorder = AudioRecorder(
            sample_rate=self.config.audio.sample_rate,
            device=device,
        )
        self.inserter = TextInserter()
        self.postprocessor = PostProcessor(
            base_url=self.config.postprocess.base_url,
            model=self.config.postprocess.model,
            timeout_seconds=self.config.postprocess.timeout_seconds,
        )
        self.lm = LMStudioManager(base_url=self.config.postprocess.base_url)

        # Thread-safe bridge: worker threads emit, GUI thread reacts
        self.bridge = GuiBridge()
        self.window = MainWindow(self.settings, self.history)
        self.tray = TrayIcon(on_show=self.window.show_and_raise, on_quit=self._on_quit)

        self.bridge.status_changed.connect(self.tray.set_state)
        self.bridge.status_changed.connect(self.window.set_status)
        self.bridge.transcribed.connect(self.window.add_transcription)
        self.bridge.lm_progress.connect(self.window.set_lm_progress)
        self.bridge.lm_models.connect(self.window.set_lm_models)
        self.bridge.lm_ready.connect(self.window.set_lm_ready)
        self.window.lm_toggle_requested.connect(self._on_toggle_postprocess)
        self.window.lm_ensure_requested.connect(self._ensure_lm_ready)
        self.window.lm_model_changed.connect(self._on_lm_model_changed)
        self.window.device_changed.connect(self._on_device_changed)

        # Heavy import deferred so the UI shows up while the model loads
        self.transcriber = None
        self.hotkey = PushToTalkHotkey(
            self.config.hotkey,
            on_press=self._on_hotkey_press,
            on_release=self._on_hotkey_release,
        )
        self._busy = threading.Lock()

    # --- lifecycle -----------------------------------------------------

    def run(self) -> int:
        self.tray.show()
        self.window.show()
        threading.Thread(target=self._load_model, daemon=True).start()
        self._probe_lm()
        return self.qt_app.exec()

    def _load_model(self) -> None:
        try:
            from transcriber import Transcriber

            self.transcriber = Transcriber(
                model_name=self.config.model.name,
                device=self.config.model.device,
                compute_type=self.config.model.compute_type,
                language=self.config.language,
                sample_rate=self.config.audio.sample_rate,
            )
            self.hotkey.start()
            self.tray.notify(f"Ready. Hold {self.config.hotkey} to dictate.")
        except Exception as e:
            logger.exception("Failed to load Whisper model")
            self.tray.notify(f"Model load failed: {e}")

    def _preferred_model(self) -> str:
        """Model key to prefer: saved selection > config > none."""
        return self.settings.get("lm_model") or self.config.postprocess.model or ""

    def _choose_loaded(self, loaded: list[str]) -> str | None:
        """Pick which loaded LLM to use: the preferred one, else the first."""
        if not loaded:
            return None
        preferred = self._preferred_model()
        if preferred and preferred in loaded:
            return preferred
        return loaded[0]

    def _probe_lm(self) -> None:
        """Reflect the current LM Studio state at startup without starting it."""

        def worker() -> None:
            if not self.lm.is_server_up():
                self.bridge.lm_progress.emit("server offline")
                self.bridge.lm_models.emit([], None)
                self.bridge.lm_ready.emit(False, None)
                return
            available = self.lm.list_llms()
            loaded_key = self._choose_loaded(self.lm.loaded_llms())
            self.bridge.lm_models.emit(available, loaded_key)
            if loaded_key:
                self.postprocessor.model = loaded_key
            else:
                self.bridge.lm_progress.emit("no model loaded")
            self.bridge.lm_ready.emit(bool(loaded_key), loaded_key)

        threading.Thread(target=worker, daemon=True).start()

    def _ensure_lm_ready(self) -> None:
        """Refresh clicked: start the server and load a model if needed."""

        def worker() -> None:
            if not self.lm.available:
                up = self.lm.is_server_up()
                self.bridge.lm_progress.emit("server online" if up else "lms CLI not found")
                self.bridge.lm_models.emit([], None)
                self.bridge.lm_ready.emit(up, None)
                return

            if not self.lm.is_server_up():
                self.bridge.lm_progress.emit("starting server...")
                self.lm.start_server()
                if not self.lm.wait_for_server():
                    self.bridge.lm_progress.emit("server failed to start")
                    self.bridge.lm_models.emit([], None)
                    self.bridge.lm_ready.emit(False, None)
                    return

            available = self.lm.list_llms()
            loaded_key = self._choose_loaded(self.lm.loaded_llms())
            self.bridge.lm_models.emit(available, loaded_key)

            if loaded_key is None:
                target = self._preferred_model()
                if target not in {key for key, _ in available}:
                    target = available[0][0] if available else ""
                if not target:
                    self.bridge.lm_progress.emit("no models available")
                    self.bridge.lm_ready.emit(False, None)
                    return
                self.bridge.lm_progress.emit(f"loading {target}...")
                if not self.lm.load_model(target):
                    self.bridge.lm_progress.emit("model load failed")
                    self.bridge.lm_ready.emit(False, None)
                    return
                loaded_key = target

            self.postprocessor.model = loaded_key
            self.settings.set("lm_model", loaded_key)
            self.bridge.lm_models.emit(available, loaded_key)
            self.bridge.lm_ready.emit(True, loaded_key)

        threading.Thread(target=worker, daemon=True).start()

    def _on_lm_model_changed(self, model_key: str) -> None:
        """User picked a different model in the dropdown - load it."""

        def worker() -> None:
            if self.lm.available and model_key not in self.lm.loaded_llms():
                self.bridge.lm_progress.emit(f"loading {model_key}...")
                if not self.lm.load_model(model_key):
                    self.bridge.lm_progress.emit("model load failed")
                    loaded_key = self._choose_loaded(self.lm.loaded_llms())
                    self.postprocessor.model = loaded_key or ""
                    self.bridge.lm_ready.emit(bool(loaded_key), loaded_key)
                    return
                self.lm.unload_other_llms(keep=model_key)
            self.postprocessor.model = model_key
            self.settings.set("lm_model", model_key)
            self.bridge.lm_ready.emit(True, model_key)

        threading.Thread(target=worker, daemon=True).start()

    def _on_quit(self) -> None:
        self.hotkey.stop()
        logger.info("Shutting down")
        self.qt_app.setProperty("really_quit", True)
        self.window.close()
        self.tray.hide()
        self.qt_app.quit()

    def _on_toggle_postprocess(self, enabled: bool) -> None:
        self.postprocessor.enabled = enabled
        logger.info("Post-processing %s", "enabled" if enabled else "disabled")

    def _on_device_changed(self, name: str | None) -> None:
        self.recorder.device = resolve_device(name)
        logger.info("Capture device set to: %s", name or "system default")

    # --- dictation flow ------------------------------------------------

    def _on_hotkey_press(self) -> None:
        if self.transcriber is None or self._busy.locked():
            return
        if self.config.sounds:
            beep(start=True)
        self.bridge.status_changed.emit("listening")
        self.recorder.start()

    def _on_hotkey_release(self) -> None:
        if not self.recorder.is_recording:
            return
        audio = self.recorder.stop()
        if self.config.sounds:
            beep(start=False)

        duration = len(audio) / self.config.audio.sample_rate
        if duration < self.config.min_record_seconds:
            logger.debug("Recording too short (%.2f s), ignored", duration)
            self.bridge.status_changed.emit("idle")
            return

        threading.Thread(target=self._process, args=(audio,), daemon=True).start()

    def _process(self, audio) -> None:
        if not self._busy.acquire(blocking=False):
            return
        try:
            self.bridge.status_changed.emit("processing")
            text = self.transcriber.transcribe(audio)
            if text:
                text = self.postprocessor.process(text)
                self.inserter.insert(text)
                self.bridge.transcribed.emit(text)
        except Exception:
            logger.exception("Processing failed")
        finally:
            self.bridge.status_changed.emit("idle")
            self._busy.release()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # Prevent multiple instances: stale locks older than 5 s are auto-removed
    lock_path = Path(tempfile.gettempdir()) / "whisper_dictation.lock"
    lock_file = QLockFile(str(lock_path))
    lock_file.setStaleLockTime(5000)
    if not lock_file.tryLock(100):
        logger.warning("Another instance is already running, exiting")
        QMessageBox.information(
            None,
            "Whisper Dictation",
            "Приложение уже запущено.\nПроверьте иконку в трее.",
        )
        sys.exit(0)

    sys.exit(DictationApp(app).run())
