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
from tray import TrayIcon

logger = logging.getLogger("main")


def configure_logging() -> None:
    # Libraries may include recognized words, prompts or response URLs even at INFO.
    allowed = {"main", "transcriber", "audio_recorder", "history", "postprocessor",
               "lmstudio", "settings", "hotkey", "config", "autostart"}
    handlers = [logging.FileHandler(Path(__file__).resolve().parents[1] / "app-technical.log",
                                    encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    for handler in handlers:
        handler.addFilter(lambda record: record.name in allowed)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        handlers=handlers, force=True)


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
        logger.info("config_loaded")

        self.settings = SettingsStore()
        self.history = HistoryStore()

        # GUI selection (state.json) wins over config.yaml; both fall back to default
        saved_device = self.settings.get("audio_device")
        device = resolve_device(saved_device) if saved_device else self.config.audio.device
        self.recorder = AudioRecorder(
            sample_rate=self.config.audio.sample_rate,
            device=device,
        )
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
        self.bridge.notice.connect(self.window.show_notice)
        self.window.notification.connect(self.tray.notify)
        self.bridge.lm_progress.connect(self.window.set_lm_progress)
        self.bridge.lm_models.connect(self.window.set_lm_models)
        self.bridge.lm_ready.connect(self.window.set_lm_ready)
        self.bridge.lm_selected.connect(self.window.remember_lm_model)
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
        self._capture_lock = threading.Lock()
        self._closing = threading.Event()

    # --- lifecycle -----------------------------------------------------

    def run(self) -> int:
        self.tray.show()
        self.window.show()
        self.window._set_lm_busy("checking...")
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
            with self._capture_lock:
                if self._closing.is_set():
                    return
                self.hotkey.start()
            self.bridge.notice.emit(f"Ready. Hold {self.config.hotkey} to dictate.")
        except FileNotFoundError:
            logger.warning("local_stt_resource_missing")
            self.bridge.notice.emit("Local Whisper files are missing. Set model.name to a complete local snapshot.")
        except Exception:
            logger.error("stt_or_hotkey_start_failed")
            self.bridge.notice.emit("Dictation unavailable. Check local Whisper weights, GPU and hotkey.")

    def _preferred_model(self) -> str:
        """Model key to prefer: saved selection > config > none."""
        return self.settings.get("lm_model") or self.config.postprocess.model or ""

    def _choose_loaded(self, loaded: dict[str, str]) -> str | None:
        """Pick which loaded LLM to use: the preferred one, else the first."""
        if not loaded:
            return None
        preferred = self._preferred_model()
        if preferred:
            return preferred if preferred in loaded else None
        return next(iter(loaded)) if len(loaded) == 1 else None

    def _probe_lm(self) -> None:
        """Reflect the current LM Studio state at startup without starting it."""

        def worker() -> None:
            if not self.lm.is_server_up():
                self.bridge.lm_progress.emit("server offline or endpoint is not local")
                self.bridge.lm_models.emit([], None)
                self.bridge.lm_ready.emit(False, None)
                return
            available = self.lm.list_llms()
            loaded = self.lm.loaded_llms()
            loaded_key = self._choose_loaded(loaded)
            self.bridge.lm_models.emit(available or [(k, k) for k in loaded], loaded_key)
            if loaded_key:
                self.postprocessor.model = loaded[loaded_key]
            else:
                self.bridge.lm_progress.emit("choose a local model or refresh")
            self.bridge.lm_ready.emit(bool(loaded_key), loaded_key)

        threading.Thread(target=worker, daemon=True).start()

    def _ensure_lm_ready(self) -> None:
        """Refresh clicked: start the server and load a model if needed."""

        def worker() -> None:
            if not self.lm.available:
                up = self.lm.is_server_up()
                loaded = self.lm.loaded_llms() if up else {}
                key = self._choose_loaded(loaded)
                self.postprocessor.model = loaded[key] if key else ""
                self.bridge.lm_progress.emit("no selected model ready" if up else "local server offline")
                self.bridge.lm_models.emit([(k, k) for k in loaded], key)
                self.bridge.lm_ready.emit(bool(key), key)
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
            loaded = self.lm.loaded_llms()
            loaded_key = self._choose_loaded(loaded)
            self.bridge.lm_models.emit(available, loaded_key)

            if loaded_key is None:
                target = self._preferred_model()
                if not target and len(available) == 1:
                    target = available[0][0]
                if target not in dict(available):
                    self.bridge.lm_progress.emit("select a downloaded model")
                    self.bridge.lm_ready.emit(False, None)
                    return
                self.bridge.lm_progress.emit("loading local model...")
                loaded = self.lm.loaded_llms() if self.lm.load_model(target) else {}
                if target not in loaded:
                    self.bridge.lm_progress.emit("model load failed")
                    self.bridge.lm_ready.emit(False, None)
                    return
                loaded_key = target

            self.postprocessor.model = loaded[loaded_key]
            self.bridge.lm_selected.emit(loaded_key)
            self.bridge.lm_models.emit(available, loaded_key)
            self.bridge.lm_ready.emit(True, loaded_key)

        threading.Thread(target=worker, daemon=True).start()

    def _on_lm_model_changed(self, model_key: str) -> None:
        """User picked a different model in the dropdown - load it."""

        def worker() -> None:
            loaded = self.lm.loaded_llms()
            if model_key not in loaded:
                self.bridge.lm_progress.emit("loading local model...")
                loaded = self.lm.loaded_llms() if self.lm.load_model(model_key) else {}
                if model_key not in loaded:
                    self.bridge.lm_progress.emit("model load failed")
                    self.postprocessor.model = ""
                    self.bridge.lm_ready.emit(False, None)
                    return
            self.postprocessor.model = loaded[model_key]
            self.bridge.lm_selected.emit(model_key)
            self.bridge.lm_ready.emit(True, model_key)

        threading.Thread(target=worker, daemon=True).start()

    def _on_quit(self) -> None:
        with self._capture_lock:
            self._closing.set()
            self.hotkey.stop()
            if self.recorder.is_recording:
                try:
                    self.recorder.stop()
                except Exception:
                    logger.warning("mic_quit_close_failed")
                finally:
                    self._busy.release()
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
        logger.info("mic_selection_changed")

    # --- dictation flow ------------------------------------------------

    def _on_hotkey_press(self) -> None:
        with self._capture_lock:
            if self._closing.is_set() or self.transcriber is None or not self._busy.acquire(False):
                return
            try:
                self.recorder.start()
            except Exception:
                self._busy.release()
                self._mic_failed()
                return
            self.bridge.status_changed.emit("listening")
            if self.config.sounds:
                beep(start=True)

    def _mic_failed(self) -> None:
        logger.warning("mic_capture_failed")
        self.bridge.status_changed.emit("idle")
        self.bridge.notice.emit("Microphone unavailable. Select an input and try again.")

    def _on_hotkey_release(self) -> None:
        with self._capture_lock:
            if self._closing.is_set() or not self.recorder.is_recording:
                return
            try:
                audio = self.recorder.stop()
            except Exception:
                self._busy.release()
                self._mic_failed()
                return
        if self.config.sounds:
            beep(start=False)

        duration = len(audio) / self.config.audio.sample_rate
        if duration < self.config.min_record_seconds:
            logger.debug("Recording too short (%.2f s), ignored", duration)
            self.bridge.status_changed.emit("idle")
            self._busy.release()
            return

        try:
            threading.Thread(target=self._process, args=(audio,), kwargs={"reserved": True}, daemon=True).start()
        except RuntimeError:
            self._busy.release()
            self.bridge.status_changed.emit("idle")
            self.bridge.notice.emit("Processing could not start. Try again.")

    def _process(self, audio, *, reserved=False) -> None:
        if not reserved and not self._busy.acquire(blocking=False):
            return
        try:
            if self._closing.is_set():
                return
            self.bridge.status_changed.emit("processing")
            text = self.transcriber.transcribe(audio)
            if text and not self._closing.is_set():
                text, outcome = self.postprocessor.process(text)
                if not self._closing.is_set():
                    self.bridge.transcribed.emit(text, outcome)
        except Exception:
            logger.error("processing_failed")
            self.bridge.notice.emit("Processing failed. Try again.")
        finally:
            self.bridge.status_changed.emit("idle")
            self._busy.release()


if __name__ == "__main__":
    configure_logging()
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
