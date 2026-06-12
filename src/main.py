"""Entry point: wires together hotkey, recorder, transcriber, post-processor, tray."""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

from audio_recorder import AudioRecorder
from config import load_config
from hotkey import PushToTalkHotkey
from postprocessor import PostProcessor
from text_inserter import TextInserter
from tray import TrayApp

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
    def __init__(self) -> None:
        self.config = load_config()
        logger.info("Config loaded: hotkey=%s, model=%s", self.config.hotkey, self.config.model.name)

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
        self.postprocessor.enabled = self.config.postprocess.enabled

        self.tray = TrayApp(
            postprocess_enabled=self.postprocessor.enabled,
            on_toggle_postprocess=self._on_toggle_postprocess,
            on_quit=self._on_quit,
        )

        # Heavy import deferred so the tray can show up while the model loads
        self.transcriber = None
        self.hotkey = PushToTalkHotkey(
            self.config.hotkey,
            on_press=self._on_hotkey_press,
            on_release=self._on_hotkey_release,
        )
        self._busy = threading.Lock()

    # --- lifecycle -----------------------------------------------------

    def run(self) -> None:
        threading.Thread(target=self._load_model, daemon=True).start()
        self.tray.run()  # blocks main thread until quit

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

    def _on_quit(self) -> None:
        self.hotkey.stop()
        logger.info("Shutting down")

    def _on_toggle_postprocess(self, enabled: bool) -> None:
        self.postprocessor.enabled = enabled
        logger.info("Post-processing %s", "enabled" if enabled else "disabled")

    # --- dictation flow ------------------------------------------------

    def _on_hotkey_press(self) -> None:
        if self.transcriber is None or self._busy.locked():
            return
        if self.config.sounds:
            beep(start=True)
        self.tray.set_state("recording")
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
            self.tray.set_state("idle")
            return

        threading.Thread(target=self._process, args=(audio,), daemon=True).start()

    def _process(self, audio) -> None:
        if not self._busy.acquire(blocking=False):
            return
        try:
            self.tray.set_state("processing")
            text = self.transcriber.transcribe(audio)
            if text:
                text = self.postprocessor.process(text)
                self.inserter.insert(text)
        except Exception:
            logger.exception("Processing failed")
        finally:
            self.tray.set_state("idle")
            self._busy.release()


if __name__ == "__main__":
    DictationApp().run()
