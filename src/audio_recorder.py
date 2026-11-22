"""Microphone capture: start/stop recording into a numpy buffer."""

from __future__ import annotations

import logging
import threading

import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)


def list_input_devices() -> list[tuple[int, str]]:
    """Return unique input devices as (index, name).

    On Windows each physical device is reported by several host APIs
    (MME, DirectSound, WASAPI, WDM-KS); MME indices are kept because
    they reliably support arbitrary sample rates (unlike WASAPI which
    may reject 16000 Hz). Falls back to name-based deduplication on
    other platforms. Virtual/remap devices are filtered out.
    """
    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
    except (sd.PortAudioError, OSError):
        logger.warning("mic_enumeration_failed")
        return []

    mme: list[tuple[int, str]] = []
    by_name: dict[str, int] = {}
    for idx, dev in enumerate(devices):
        if dev["max_input_channels"] < 1:
            continue
        name = dev["name"].strip()
        api = hostapis[dev["hostapi"]]["name"]
        if "MME" in api:
            if "переназначение" in name.lower() or "remap" in name.lower():
                continue
            mme.append((idx, name))
        by_name.setdefault(name, idx)

    if mme:
        return mme
    return [(idx, name) for name, idx in by_name.items()]


def resolve_device(name: str | None, sample_rate: int = 16000) -> int | None:
    """Map a saved device name to a current device index (None = default)."""
    if not name:
        return None
    for idx, dev_name in list_input_devices():
        if dev_name == name:
            return idx
    # Keep valid PortAudio selectors (including a host API or a unique substring).
    try:
        index = sd.query_devices(name, "input")["index"]
        sd.check_input_settings(device=index, channels=1, dtype="float32", samplerate=sample_rate)
        return index
    except (sd.PortAudioError, ValueError, KeyError):
        pass
    logger.warning("mic_selection_unavailable")
    return None


class AudioRecorder:
    """Records 16 kHz mono float32 audio from the microphone."""

    def __init__(self, sample_rate: int = 16000, device: int | str | None = None,
                 max_seconds: int = 120):
        self.sample_rate = sample_rate
        self.device = resolve_device(device, sample_rate) if isinstance(device, str) else device
        self.max_frames = min(sample_rate * max_seconds, 16 * 1024 * 1024 // 4)
        self._buffer = None
        self._frames = 0
        self.limit_reached = False
        self._stream: sd.InputStream | None = None
        self._lock = threading.Lock()
        self._recording = False

    @property
    def is_recording(self) -> bool:
        return self._recording

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            logger.warning("audio_stream_status")
        with self._lock:
            if not self._recording or self.limit_reached:
                return
            count = len(indata)
            if self._frames + count > self.max_frames:
                self.limit_reached = True
                raise sd.CallbackStop
            self._buffer[self._frames:self._frames + count] = indata[:, 0]
            self._frames += count

    def start(self) -> None:
        """Begin capturing audio. No-op if already recording."""
        if self._recording:
            return
        with self._lock:
            self._buffer = np.empty(self.max_frames, dtype=np.float32)
            self._frames = 0
            self.limit_reached = False
            self._recording = True
        try:
            self._stream = sd.InputStream(
                samplerate=self.sample_rate, channels=1, dtype="float32",
                device=self.device, callback=self._callback,
            )
            self._stream.start()
        except Exception:
            with self._lock:
                self._recording = False
                self._buffer = None
            stream, self._stream = self._stream, None
            if stream is not None:
                stream.close()
            raise
        logger.info("recording_started")

    def stop(self) -> np.ndarray:
        """Stop capturing and return the recorded audio as float32 mono array."""
        with self._lock:
            self._recording = False
            buffer, self._buffer = self._buffer, None
            count = self._frames
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()
        audio = buffer[:count] if buffer is not None else np.zeros(0, dtype=np.float32)
        logger.debug("Recording stopped: %.2f s", len(audio) / self.sample_rate)
        return audio
