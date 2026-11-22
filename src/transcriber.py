"""Speech-to-text via faster-whisper on GPU."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# Minimum RMS energy below which audio is treated as silence
SILENCE_RMS_THRESHOLD = 0.005


def _add_nvidia_dll_dirs() -> None:
    """Make pip-installed cuBLAS/cuDNN DLLs discoverable on Windows."""
    if os.name != "nt":
        return
    for pkg in ("cublas", "cudnn"):
        try:
            module = __import__(f"nvidia.{pkg}", fromlist=[pkg])
            bin_dir = Path(module.__path__[0]) / "bin"
            if bin_dir.exists():
                os.add_dll_directory(str(bin_dir))
                os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
        except ImportError:
            logger.debug("nvidia.%s not installed, skipping DLL dir setup", pkg)


_add_nvidia_dll_dirs()

# Disable SDK background traffic in the normal launcher as well as the probes.
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

from faster_whisper import WhisperModel  # noqa: E402
from faster_whisper.utils import download_model  # noqa: E402


class Transcriber:
    """Wraps faster-whisper with model warm-up and silence filtering."""

    def __init__(
        self,
        model_name: str = "large-v3-turbo",
        device: str = "cuda",
        compute_type: str = "int8_float16",
        language: str = "auto",
        sample_rate: int = 16000,
    ):
        self.language = None if language == "auto" else language
        self.sample_rate = sample_rate
        # A missing tokenizer makes Faster Whisper call Tokenizer.from_pretrained
        # even when given a local model directory.
        if str(model_name).startswith(("\\\\", "//")):
            raise ValueError("A local model directory is required")
        folder = Path(model_name)
        if not folder.is_dir():
            try:
                folder = Path(download_model(model_name, local_files_only=True))
            except (OSError, ValueError):
                raise FileNotFoundError("Whisper model unavailable in local cache") from None
        for name in ("model.bin", "tokenizer.json", "config.json"):
            if not (folder / name).is_file():
                raise FileNotFoundError("Incomplete local Whisper model")
        self.model = WhisperModel(str(folder), device=device, compute_type=compute_type,
                                  local_files_only=True)
        self._warmup()
        logger.info("Model loaded and warmed up")

    def _warmup(self) -> None:
        """Run a dummy transcription so the first real request is fast."""
        silence = np.zeros(self.sample_rate // 2, dtype=np.float32)
        segments, _ = self.model.transcribe(silence, language="en")
        list(segments)

    def transcribe(self, audio: np.ndarray) -> str:
        """Transcribe a float32 mono 16 kHz buffer. Returns empty string for silence."""
        if len(audio) == 0:
            return ""
        rms = float(np.sqrt(np.mean(audio**2)))
        if rms < SILENCE_RMS_THRESHOLD:
            logger.debug("Audio below silence threshold (rms=%.5f), skipping", rms)
            return ""

        segments, _ = self.model.transcribe(
            audio,
            language=self.language,
            vad_filter=True,
            beam_size=5,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        logger.info("stt_complete chars=%d", len(text))
        return text
