"""Opt-in local Raw/Smart integration with synthetic speech and a disposable profile."""
import argparse
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np
from PySide6.QtWidgets import QApplication

from history import HistoryStore
from main import DictationApp
from transcriber import Transcriber


def synthetic_speech():
    phrase = "Um Alice please do not move the seven blue folders. Are they ready?"
    command = ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
               f"flite=text='{phrase}':voice=slt", "-ar", "16000", "-ac", "1", "-f", "f32le", "pipe:1"]
    raw = subprocess.run(command, check=True, stdout=subprocess.PIPE, timeout=20).stdout
    return np.frombuffer(raw, dtype="<f4")


def expected_meaning(text):
    words = text.lower().replace("don’t", "do not").replace("don't", "do not")
    words = re.findall(r"[a-z0-9]+", words.replace("7", "seven"))
    if words[:1] in (["um"], ["uh"]):
        words = words[1:]
    return words == "alice please do not move the seven blue folders are they ready".split()


def run(profile, instance, cpu):
    qt = QApplication.instance() or QApplication([])
    database = profile / "artifacts.sqlite3"
    app = DictationApp(qt, database=database, legacy_dir=profile)
    try:
        if instance and (not app.lm.is_server_up() or instance not in app.lm.loaded_llms().values()):
            raise RuntimeError("selected local instance is not ready")
        pcm = synthetic_speech()
        model = app.config.model
        device = "cpu" if cpu else model.device
        compute = "int8" if cpu else model.compute_type
        app.transcriber = Transcriber(model.name, device, compute, app.config.language)
        app.postprocessor.model = instance or ""
        for smart in ((False, True) if instance else (False,)):
            app.postprocessor.enabled = smart
            started = monotonic()
            app._process(pcm)
            qt.processEvents()
            entry = app.history.page()[0]
            storage = "result" if smart else "original"
            status = (entry["processing"], entry["storage"], entry["delivery"])
            elapsed = monotonic() - started
            print(f"{'Smart' if smart else 'Raw'} {device}/{compute} {'/'.join(status)} "
                  f"elapsed={elapsed:.2f}s changed={entry['original'] != entry['text']} "
                  f"meaning={expected_meaning(entry['text'])}", flush=True)
            if status != ("ready", storage, "not_requested"):
                raise RuntimeError(f"inference failed ({entry['error']})")
            if app.window.text_edit.toPlainText() != entry["text"] or not expected_meaning(entry["original"]):
                raise RuntimeError("unexpected displayed text or transcription of synthetic speech")
            if smart and (not expected_meaning(entry["text"]) or not entry["text"].endswith("?")):
                raise RuntimeError("synthetic correction changed the command or question")
        entries = app.history.page()
        if len(entries) != (2 if instance else 1):
            raise RuntimeError("unexpected artifact count")
        if instance:
            # Keep the port reserved but do not listen. The shared service stays available.
            with socket.socket() as offline:
                offline.bind(("127.0.0.1", 0))
                port = offline.getsockname()[1]
                app.postprocessor.base_url = f"http://127.0.0.1:{port}/v1"
                app._process(pcm)
            qt.processEvents()
            fallback, *previous = app.history.page()
            if (previous != entries or fallback["processing"] != "fallback" or fallback["storage"] != "original"
                    or fallback["error"] != "smart_offline" or fallback["delivery"] != "not_requested"
                    or fallback["text"] != fallback["original"] or not expected_meaning(fallback["text"])
                    or "Smart fallback" not in app.window.result_label.text()):
                raise RuntimeError("offline fallback failed to preserve Original or previous success")
            entries = [fallback, *previous]
            print("Smart offline fallback/original/not_requested previous_success=preserved", flush=True)
    finally:
        app._shutdown()
    restored = HistoryStore(database, profile)
    try:
        if restored.page() != entries:
            raise RuntimeError("restart changed saved artifacts")
        print(f"Restart verified artifacts={len(entries)}", flush=True)
    finally:
        restored.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", help="Exact ID of an already loaded LM Studio instance")
    parser.add_argument("--cpu", action="store_true", help="Use CPU int8 STT")
    options = parser.parse_args()
    with TemporaryDirectory(prefix="whisper-m1c-") as directory:
        run(Path(directory), options.instance, options.cpu)
