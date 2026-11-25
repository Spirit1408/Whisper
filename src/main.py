"""Entry point: wires together GUI, tray, hotkey, recorder, transcriber, post-processor."""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
import threading
from pathlib import Path

from PySide6.QtCore import QLockFile
from PySide6.QtWidgets import QApplication, QMessageBox

from audio_recorder import AudioRecorder, resolve_device
from config import load_config
from dictation import Coordinator
from delivery import WindowsDelivery
from gui import GuiBridge, MainWindow
from history import HistoryStore, StorageError
from hotkey import PushToTalkHotkey
from lmstudio import LMStudioManager
from postprocessor import PostProcessor
from settings import SettingsStore
from tray import TrayIcon

logger = logging.getLogger("main")


def configure_logging(directory=None) -> None:
    # Libraries may include recognized words, prompts or response URLs even at INFO.
    allowed = {"main", "transcriber", "audio_recorder", "history", "postprocessor", "dictation",
               "lmstudio", "settings", "hotkey", "config", "autostart"}
    handlers = []
    try:
        log_dir = directory if directory is not None else Path(__file__).resolve().parents[1]
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_dir / "app-technical.log", encoding="utf-8"))
    except OSError:
        handlers.append(logging.NullHandler())
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
    def __init__(self, qt_app: QApplication, *, database=None, legacy_dir=None) -> None:
        self.qt_app = qt_app
        self.config = load_config()
        logger.info("config_loaded")

        self.history = HistoryStore(database) if legacy_dir is None else HistoryStore(database, legacy_dir)
        self.settings = SettingsStore(self.history)

        # A saved system-default choice (None) also overrides the configured device.
        device = self.settings.get("audio_device", self.config.audio.device)
        self.recorder = AudioRecorder(
            sample_rate=self.config.audio.sample_rate,
            device=device,
        )
        self.postprocessor = PostProcessor(
            base_url=self.config.postprocess.base_url,
            model="",  # Resolve the configured key to a ready local instance before inference.
            timeout_seconds=self.config.postprocess.timeout_seconds,
        )
        self._closing = threading.Event()
        self.lm = LMStudioManager(base_url=self.config.postprocess.base_url, closing=self._closing)
        self._loaded_models = {}

        # Thread-safe bridge: worker threads emit, GUI thread reacts
        self.bridge = GuiBridge()
        self.jobs = Coordinator(self.history, self.bridge.transcribed.emit,
                                WindowsDelivery(self.config.excluded_processes), self.bridge.notice.emit)
        self.window = MainWindow(self.settings, self.history, self.jobs.ram_results)
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
        self.window.cancel_requested.connect(self._on_cancel)
        self.window.repeat_requested.connect(self._on_repeat)
        self.window.draft_started.connect(self._on_draft)
        self.window.manual_delivery_requested.connect(self.jobs.disable_auto)
        self.bridge.presets_used.connect(self.window.presets.refresh)
        self.postprocessor.enabled = self.window.selection.active != "raw"

        # Heavy import deferred so the UI shows up while the model loads
        self.transcriber = None
        self.hotkey = PushToTalkHotkey(
            self.config.hotkey,
            on_press=self._on_hotkey_press,
            on_release=self._on_hotkey_release,
        )
        self._capture_job = None
        self._worker = None
        self._capture_lock = threading.Lock()

    # --- lifecycle -----------------------------------------------------

    def run(self) -> int:
        self.tray.show()
        self.window.show()
        self.window._set_lm_busy("checking...")
        threading.Thread(target=self._load_model, daemon=True).start()
        self._probe_lm()
        self.qt_app.aboutToQuit.connect(self._shutdown)
        try:
            return self.qt_app.exec()
        finally:
            self._shutdown()
            self.qt_app.aboutToQuit.disconnect(self._shutdown)

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
        self.postprocessor.model = ""
        self._loaded_models = {}

        def worker() -> None:
            if not self.lm.is_server_up():
                self.bridge.lm_progress.emit("server offline or endpoint is not local")
                self.bridge.lm_models.emit([], None)
                self.bridge.lm_ready.emit(False, None)
                return
            available = self.lm.list_llms()
            loaded = self._read_loaded()
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
        self.postprocessor.model = ""
        self._loaded_models = {}

        def worker() -> None:
            if not self.lm.available:
                up = self.lm.is_server_up()
                loaded = self._read_loaded() if up else {}
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
            loaded = self._read_loaded()
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
                loaded = self._read_loaded() if self.lm.load_model(target) else {}
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
        self.postprocessor.model = ""
        self._loaded_models = {}

        def worker() -> None:
            loaded = self._read_loaded()
            if model_key not in loaded:
                self.bridge.lm_progress.emit("loading local model...")
                loaded = self._read_loaded() if self.lm.load_model(model_key) else {}
                if model_key not in loaded:
                    self.bridge.lm_progress.emit("model load failed")
                    self.postprocessor.model = ""
                    self.bridge.lm_ready.emit(False, None)
                    return
            self.postprocessor.model = loaded[model_key]
            self.bridge.lm_selected.emit(model_key)
            self.bridge.lm_ready.emit(True, model_key)

        threading.Thread(target=worker, daemon=True).start()

    def _read_loaded(self):
        self._loaded_models = self.lm.loaded_llms()
        return self._loaded_models

    def _stop_capture(self):
        job, self._capture_job = self._capture_job, None
        if job is None:
            return
        try:
            self.recorder.stop()
        except Exception:
            logger.warning("mic_quit_close_failed")
        finally:
            self.jobs.release(job)

    def _on_cancel(self):
        with self._capture_lock:
            if self.jobs.cancel():
                self._stop_capture()
                self.window.refresh_history()
                self.bridge.notice.emit("Cancelled. Any saved Original is in history.")
                state = "idle" if self._worker is None else "Waiting for cancelled backend"
                self.bridge.status_changed.emit(state)

    def _shutdown(self) -> None:
        with self._capture_lock:
            if self._closing.is_set():
                return
            self._closing.set()
            self.jobs.cancel(closing=True)
            self._stop_capture()
            try:
                self.hotkey.stop()
            except Exception:
                logger.warning("hotkey_quit_close_failed")
            worker = self._worker
        if worker is not None:
            worker.join(timeout=1.0)
        self.history.close()
        logger.info("Shutting down")

    def _on_quit(self) -> None:
        self._shutdown()
        self.qt_app.setProperty("really_quit", True)
        self.window.close()
        self.tray.hide()
        self.qt_app.quit()

    def _on_toggle_postprocess(self, enabled: bool) -> None:
        self.postprocessor.enabled = enabled
        logger.info("Post-processing %s", "enabled" if enabled else "disabled")

    def _on_device_changed(self, name: str | None) -> None:
        self.recorder.device = resolve_device(name, self.recorder.sample_rate)
        logger.info("mic_selection_changed")

    # --- dictation flow ------------------------------------------------

    def _admit(self, *, auto=False, repeat=None):
        state = self.window.selection
        with state.lock:
            active = state.active
            if self.settings.get("active_preset") is None:
                active = "strict" if self.postprocessor.enabled else "raw"
            preset = state.definitions.get(state.once or active)
            if preset is None:
                self.bridge.notice.emit("Preset unavailable. Select an available preset.")
                return None
            processor = self.postprocessor.bind(preset, self._loaded_models)
            job = self.jobs.admit(processor, auto=auto and self.settings.get("delivery_mode") == "auto")
            if job is None:
                self.bridge.notice.emit("Busy. Try again after processing.")
                return None
            if repeat:
                try:
                    self.jobs.prepare_repeat(job, *repeat)
                except StorageError:
                    self.jobs.release(job)
                    self.bridge.notice.emit("Source unavailable or changed. Reopen the saved version.")
                    return None
            if state.once:
                state.once = None
                self.bridge.presets_used.emit()
            return job

    def _on_draft(self, artifact):
        with self._capture_lock:
            job = self.jobs.active
            if job and (job.repeat_artifact or job.id) == artifact:
                self.jobs.cancel()

    def _on_repeat(self, artifact, source, revision):
        with self._capture_lock:
            job = self._admit(repeat=(artifact, source, revision))
            if job:
                self._start_worker(None, job)

    def _on_hotkey_press(self) -> None:
        with self._capture_lock:
            if self._closing.is_set() or self.transcriber is None:
                return
            job = self._admit(auto=True)
            if job is None:
                return
            try:
                self.recorder.start()
            except Exception:
                self.jobs.release(job)
                self._mic_failed()
                return
            self._capture_job = job
            self.jobs.started_capture(job)
            self.bridge.status_changed.emit("listening")
            if self.config.sounds:
                beep(start=True)

    def _mic_failed(self) -> None:
        logger.warning("mic_capture_failed")
        self.bridge.status_changed.emit("idle")
        self.bridge.notice.emit("Microphone unavailable. Select an input and try again.")

    def _on_hotkey_release(self) -> None:
        with self._capture_lock:
            job, self._capture_job = self._capture_job, None
            if job is None:
                return
            try:
                audio = self.recorder.stop()
            except Exception:
                self.jobs.finish_capture(job, "failed", "mic_stop_failed")
                self._mic_failed()
                return
            if self.config.sounds:
                beep(start=False)
            if self.recorder.limit_reached or len(audio) < self.config.min_record_seconds * self.config.audio.sample_rate:
                reason = "pcm_limit" if self.recorder.limit_reached else "short_capture"
                self.jobs.finish_capture(job, "discarded", reason)
                self.bridge.status_changed.emit("idle")
                if self.recorder.limit_reached:
                    self.bridge.notice.emit("Recording limit exceeded. Try a shorter dictation.")
                return
            self._start_worker(audio, job)

    def _start_worker(self, audio, job):
        self._worker = threading.Thread(target=self._process, args=(audio, job), daemon=True)
        try:
            self._worker.start()
        except RuntimeError:
            self._worker = None
            self.jobs.finish_capture(job, "failed", "worker_not_started")
            self.bridge.status_changed.emit("idle")
            self.bridge.notice.emit("Processing could not start. Try again.")

    def _process(self, audio, job=None) -> None:
        if job is None:
            job = self._admit()
        if job is None:
            return
        try:
            if not self._closing.is_set():
                self.bridge.status_changed.emit("processing")
                if job.repeat_artifact:
                    self.jobs.run_repeat(job)
                else:
                    self.jobs.run(job, audio, self.transcriber.transcribe)
        except Exception:
            logger.error("processing_failed")
            if not self._closing.is_set():
                self.bridge.notice.emit("Processing failed. Try again.")
        finally:
            with self._capture_lock:
                self.jobs.release(job)
                self._worker = None
                if not self._closing.is_set():
                    self.bridge.status_changed.emit("idle")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile-dir", type=Path, help="Use this directory for SQLite, legacy inputs and log")
    options, qt_args = parser.parse_known_args()
    configure_logging(options.profile_dir)
    app = QApplication([sys.argv[0], *qt_args])
    app.setQuitOnLastWindowClosed(False)

    # A live process owns the database for its entire session.
    lock_path = Path(tempfile.gettempdir()) / "whisper_dictation.lock"
    lock_file = QLockFile(str(lock_path))
    lock_file.setStaleLockTime(0)
    if not lock_file.tryLock(100):
        logger.warning("Another instance is already running, exiting")
        QMessageBox.information(
            None,
            "Whisper Dictation",
            "Приложение уже запущено.\nПроверьте иконку в трее.",
        )
        sys.exit(0)

    profile = options.profile_dir
    database = profile / "artifacts.sqlite3" if profile is not None else None
    sys.exit(DictationApp(app, database=database, legacy_dir=profile).run())
