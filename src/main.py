"""Entry point: wires together GUI, tray, hotkey, recorder, transcriber, post-processor."""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

from PySide6.QtWidgets import QApplication

from audio_recorder import AudioRecorder
from config import load_config
from gui import GuiBridge, MainWindow
from history import HistoryStore
from hotkey import PushToTalkHotkey
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

        self.recorder = AudioRecorder(
            sample_rate=self.config.audio.sample_rate,
            device=self.config.audio.device,
        )
        self.inserter = TextInserter()
        self.postprocessor = PostProcessor(
            base_url=self.config.postprocess.base_url,
            model=self.config.postprocess.model,
            timeout_seconds=self.config.postprocess.timeout_seconds,
        )

        # Thread-safe bridge: worker threads emit, GUI thread reacts
        self.bridge = GuiBridge()
        self.window = MainWindow(self.settings, self.history)
        self.tray = TrayIcon(on_show=self.window.show_and_raise, on_quit=self._on_quit)

        self.bridge.state_changed.connect(self.tray.set_state)
        self.bridge.transcribed.connect(self.window.add_transcription)
        self.bridge.lm_status.connect(self.window.set_lm_available)
        self.window.lm_toggle_requested.connect(self._on_toggle_postprocess)
        self.window.lm_recheck_requested.connect(self._check_lm_server)

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
        self._check_lm_server()
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

    def _check_lm_server(self) -> None:
        """Probe LM Studio availability in the background, report via signal."""

        def probe() -> None:
            self.bridge.lm_status.emit(self.postprocessor.check_server())

        threading.Thread(target=probe, daemon=True).start()

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

    # --- dictation flow ------------------------------------------------

    def _on_hotkey_press(self) -> None:
        if self.transcriber is None or self._busy.locked():
            return
        if self.config.sounds:
            beep(start=True)
        self.bridge.state_changed.emit("recording")
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
            self.bridge.state_changed.emit("idle")
            return

        threading.Thread(target=self._process, args=(audio,), daemon=True).start()

    def _process(self, audio) -> None:
        if not self._busy.acquire(blocking=False):
            return
        try:
            self.bridge.state_changed.emit("processing")
            text = self.transcriber.transcribe(audio)
            if text:
                text = self.postprocessor.process(text)
                self.inserter.insert(text)
                self.bridge.transcribed.emit(text)
        except Exception:
            logger.exception("Processing failed")
        finally:
            self.bridge.state_changed.emit("idle")
            self._busy.release()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    sys.exit(DictationApp(app).run())
