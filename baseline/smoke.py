"""Check the two successful baseline paths with local adapters and synthetic inputs."""

import ast
import importlib.metadata as metadata
import json
import logging
import os
import re
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

ROOT = Path(__file__).resolve().parent.parent


def pin(name, version):
    return re.sub(r"[-_.]+", "-", name).lower() + "==" + version


def check_environment():
    lines = (ROOT / "requirements-baseline.txt").read_text(encoding="utf-8").splitlines()
    expected = {pin(*line.split("==")) for line in lines if line and not line.startswith("#")}
    installed = {pin(d.metadata["Name"], d.version) for d in metadata.distributions()}
    if expected != installed:
        raise RuntimeError("The installed distributions differ from the baseline lock")
    print(f"PASS environment: {len(expected)} exact distributions")


def deny_network(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
        raise RuntimeError("Network disabled in baseline smoke")


def check_dictation(root):
    from PySide6.QtWidgets import QApplication
    import main
    import numpy as np
    import transcriber

    qt = QApplication([])
    with patch("gui.list_input_devices", return_value=[]), patch("autostart.is_enabled", return_value=False):
        app = main.DictationApp(qt)

    raw, edited = "Sample alpha", "Sample alpha."
    segment = SimpleNamespace(text=raw)
    info = SimpleNamespace(language="en", language_probability=1.0)
    with patch.object(transcriber, "WhisperModel") as model, patch.object(app.hotkey, "start") as hook, patch.object(app.tray, "notify"):
        model.return_value.transcribe.return_value = ([segment], info)
        app._load_model()
        hook.assert_called_once_with()
        assert app.transcriber is not None
        audio = np.full(8000, 0.1, dtype=np.float32)
        response = Mock()
        response.json.return_value = {"choices": [{"finish_reason": "stop", "message": {"content": edited}}]}
        with patch("requests.post", return_value=response) as post, patch("pyperclip.paste", return_value="previous"), patch("pyperclip.copy") as copy, patch("keyboard.send") as send:
            app._process(audio)
            post.assert_not_called()
            app.window.set_lm_ready(True, None)
            app.window.lm_cb.setChecked(True)
            app._process(audio)
            post.assert_called_once()
            assert copy.call_args_list == [call(raw), call("previous"), call(edited), call("previous")]
            assert send.call_args_list == [call("ctrl+v"), call("ctrl+v")]
        assert model.return_value.transcribe.call_count == 3  # warm-up, Raw, Smart

    qt.processEvents()
    saved = json.loads((root / "history.json").read_text(encoding="utf-8"))
    assert [entry["text"] for entry in saved] == [edited, raw]
    assert len(app.window._cards) == 2
    for source in (root / "src").glob("*.py"):
        assert Path(sys.modules[source.stem].__file__).resolve() == source
    print("PASS Raw + Smart: real app flow, Qt/history, mocked inference, HTTP and OS input")


def run():
    if not __debug__:
        raise RuntimeError("Run without Python optimization so checks remain enabled")
    sys.addaudithook(deny_network)
    check_environment()
    with TemporaryDirectory(prefix="whisper-m0a-smoke-") as scratch:
        root = Path(scratch).resolve()
        (root / "src").mkdir()
        for source in sorted((ROOT / "src").glob("*.py")):
            ast.parse(source.read_text(encoding="utf-8-sig"), filename=source.name)
            shutil.copyfile(source, root / "src" / source.name)
        shutil.copyfile(ROOT / "config.yaml", root / "config.yaml")
        sys.path.insert(0, str(root / "src"))
        os.environ.update(QT_QPA_PLATFORM="offscreen", HF_HUB_OFFLINE="1",
                          HF_HUB_DISABLE_TELEMETRY="1", HF_HOME=str(root / "hf"))
        previous = Path.cwd()
        logging.disable(logging.CRITICAL)
        try:
            os.chdir(root)
            check_dictation(root)
        finally:
            os.chdir(previous)
            logging.shutdown()


if __name__ == "__main__":
    run()
