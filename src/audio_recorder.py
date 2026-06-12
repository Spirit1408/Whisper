"""Microphone capture: start/stop recording into a numpy buffer."""

from __future__ import annotations

import logging
import threading

import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)


class AudioRecorder:
    """Records 16 kHz mono float32 audio from the microphone."""

    def __init__(self, sample_rate: int = 16000, device: int | str | None = None):
        self.sample_rate = sample_rate
        self.device = device
        self._chunks: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None
        self._lock = threading.Lock()
        self._recording = False

    @property
    def is_recording(self) -> bool:
        return self._recording

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            logger.warning("Audio stream status: %s", status)
        with self._lock:
            if self._recording:
                self._chunks.append(indata[:, 0].copy())

    def start(self) -> None:
        """Begin capturing audio. No-op if already recording."""
        if self._recording:
            return
        with self._lock:
            self._chunks = []
            self._recording = True
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            device=self.device,
            callback=self._callback,
        )
        self._stream.start()
        logger.debug("Recording started")

    def stop(self) -> np.ndarray:
        """Stop capturing and return the recorded audio as float32 mono array."""
        with self._lock:
            self._recording = False
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        with self._lock:
            audio = (
                np.concatenate(self._chunks)
                if self._chunks
                else np.zeros(0, dtype=np.float32)
            )
            self._chunks = []
        logger.debug("Recording stopped: %.2f s", len(audio) / self.sample_rate)
        return audio
