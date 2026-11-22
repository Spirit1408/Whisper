"""D4 component probe. Optional arguments are a local model directory and a synthetic WAV."""
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import wave

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "packages"))
os.environ.update(HF_HUB_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1",
                  QT_PLUGIN_PATH=str(ROOT / "packages/PySide6/plugins"))


def offline(event, args):
    if event in ("socket.connect", "socket.getaddrinfo", "socket.sendto"):
        raise RuntimeError("Network disabled")


def probe():
    sys.addaudithook(offline)
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QLabel
    app = QApplication([])
    label = QLabel("M0 packaging probe")
    label.setAttribute(Qt.WA_DontShowOnScreen)
    label.show()
    app.processEvents()
    assert label.winId() and app.platformName() == "windows"
    print("PASS Qt Windows widget", flush=True)
    import ctypes
    import sounddevice
    import ctranslate2
    from faster_whisper.vad import get_vad_model
    from transcriber import Transcriber
    assert sounddevice.get_portaudio_version()[0] > 0
    get_vad_model()
    for pkg, dll in (("cublas", "cublas64_12.dll"), ("cudnn","cudnn64_9.dll")):
        ctypes.WinDLL(str(ROOT / "packages/nvidia" / pkg / "bin" / dll))
    print("PASS PortAudio, CT2, CUDA libraries and VAD", flush=True)
    try:
        Transcriber("large-v3-turbo")
    except (ValueError, FileNotFoundError, OSError):
        print("PASS missing weights offline", flush=True)
    else:
        raise AssertionError("Missing weights unexpectedly loaded")
    if len(sys.argv) == 3:
        import numpy as np
        with wave.open(sys.argv[2], "rb") as audio:
            assert (audio.getnchannels(), audio.getframerate(), audio.getsampwidth()) == (1, 16000, 2)
            pcm = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").astype("float32") / 32768
        stt = Transcriber(sys.argv[1])
        if not stt.transcribe(pcm):
            raise RuntimeError("Empty STT output")
        print("PASS real offline GPU dictation", flush=True)


if __name__ == "__main__":
    with TemporaryDirectory(prefix="m0-empty-model-cache-") as cache:
        os.environ["HF_HOME"] = cache
        os.environ["HF_HUB_CACHE"] = str(Path(cache) / "hub")
        probe()
