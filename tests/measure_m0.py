"""Bounded GPU probe. Pass two synthetic 16 kHz PCM WAVs and a local snapshot."""
import argparse
import json
import logging
import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch
import wave


def restrict_network(event, args):
    if event == "socket.getaddrinfo" and args[0] != "127.0.0.1":
        raise RuntimeError("Non-loopback lookup")
    if event == "socket.connect" and args[1][0] != "127.0.0.1":
        raise RuntimeError("Non-loopback connection")


def measure(args, scratch):
    root = Path(scratch)
    shutil.copytree(args.source / "src", root / "src", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copyfile(args.source / "config.yaml", root / "config.yaml")
    sys.path.insert(0, str(root / "src"))
    os.environ.update(QT_QPA_PLATFORM="offscreen", HF_HUB_OFFLINE="1",
                      HF_HUB_DISABLE_TELEMETRY="1", NO_PROXY="*", LOCALAPPDATA=str(root))
    sys.addaudithook(restrict_network)
    logging.disable(logging.CRITICAL)
    from PySide6.QtWidgets import QApplication
    import numpy as np
    import main
    from transcriber import Transcriber

    qt = QApplication([])
    with patch("gui.list_input_devices", return_value=[]), patch("autostart.is_enabled", return_value=False):
        app = main.DictationApp(qt)
    app.transcriber = Transcriber(str(args.snapshot.resolve()))
    print(json.dumps({"ready_s": round(perf_counter() - args.start, 3)}), flush=True)
    # Only the explicit live option can use a previously loaded instance.
    if args.ready_model:
        import requests
        base = "http://127.0.0.1:1234"
        with requests.Session() as client:
            client.trust_env = False
            result = client.get(base + "/api/v0/models", allow_redirects=False, timeout=2)
            if result.status_code != 200 or not any(
                m.get("id") == args.ready_model and m.get("state") == "loaded" and m.get("type") == "llm"
                for m in result.json()["data"]
            ):
                raise RuntimeError("Requested instance is not reported as loaded")
        app.postprocessor.base_url = base + "/v1"
        app.postprocessor.model = args.ready_model
    else:
        app.postprocessor.base_url = "http://127.0.0.1:1/v1"
        app.postprocessor.model = "offline-probe"
    def entries():
        return app.history.page() if hasattr(app.history, "page") else app.history.entries

    for wav in args.wav:
        with wave.open(str(wav), "rb") as f:
            assert (f.getnchannels(), f.getframerate(), f.getsampwidth()) == (1, 16000, 2)
            pcm = np.frombuffer(f.readframes(f.getnframes()), dtype="<i2").astype("float32") / 32768
        for smart in (False, True):
            app.postprocessor.enabled = smart
            for _ in range(3):
                start = perf_counter()
                stamps = {}
                def timed(name, method):
                    def record(*a, **kw):
                        begin = perf_counter()
                        value = method(*a, **kw)
                        stamps[name] = round((perf_counter() - begin) * 1000, 2)
                        return value
                    return record

                count = len(entries())
                import requests
                captured = {}
                original_send = requests.adapters.HTTPAdapter.send
                def observe_send(adapter, prepared, **kwargs):
                    reply = original_send(adapter, prepared, **kwargs)
                    if reply.ok:
                        try:
                            first = reply.json()["choices"][0]
                            content = first["message"]["content"]
                            captured["complete"] = (first["finish_reason"] == "stop"
                                                    and isinstance(content, str) and bool(content.strip()))
                        except (ValueError, TypeError, KeyError, IndexError):
                            captured["complete"] = False
                    return reply
                with patch.object(app.transcriber, "transcribe", timed("stt_ms", app.transcriber.transcribe)), \
                     patch.object(app.postprocessor, "process", timed("pp_ms", app.postprocessor.process)), \
                     patch.object(requests.adapters.HTTPAdapter, "send", observe_send), \
                     patch("pyperclip.copy"), patch("pyperclip.paste", return_value="probe"), \
                     patch("keyboard.send") as dispatch:
                    start = perf_counter()
                    app._process(pcm)
                    qt.processEvents()
                    total = (perf_counter() - start) * 1000
                assert len(entries()) == count + 1 and entries()[0]["text"].strip()
                print(json.dumps(dict(seconds=len(pcm)/16000, smart=smart, total_ms=round(total, 2),
                                      complete_llm=captured.get("complete", False),
                                      dispatch=dispatch.call_count, **stamps)), flush=True)
    if hasattr(app.history, "close"):
        app.history.close()


if __name__ == "__main__":
    started = perf_counter()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("wav", type=Path, nargs=2)
    parser.add_argument("--ready-model", help="Optional resident LM Studio ID on numeric loopback")
    args = parser.parse_args()
    args.start = started
    with TemporaryDirectory(prefix="whisper-m0-e-") as directory:
        try:
            measure(args, directory)
        finally:
            logging.shutdown()
