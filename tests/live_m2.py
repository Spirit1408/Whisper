"""Opt-in loopback integration. Only uses an explicitly named, already loaded instance."""
import argparse
from dataclasses import replace
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtWidgets import QApplication
from history import HistoryStore
from main import DictationApp
from presets import new_custom


def run(profile, instance, override_only=False):
    qt = QApplication.instance() or QApplication([])
    path = profile / "artifacts.sqlite3"
    app = DictationApp(qt, database=path, legacy_dir=profile)
    source = "Um Alice, do not move the 7 blue folders. Bob will check them on Friday. Are they ready?"
    if override_only:
        source = "Do not move the 7 folders."
    try:
        loaded = app._read_loaded()
        if instance not in loaded.values():
            raise RuntimeError("instance is not already loaded")
        app.postprocessor.model = "" if override_only else instance
        app.postprocessor.timeout = 60
        window = app.window
        controls = window.presets
        key = next(key for key, value in loaded.items() if value == instance)
        translator = replace(window.selection.definitions["translator"], language="Russian", model=key,
                             timeout=120 if override_only else 0)
        if not controls._save(translator, translator.revision):
            raise RuntimeError("translator configuration failed")
        custom = replace(new_custom(), instruction="Return only the names of the people mentioned, on one line.")
        if not controls._save(custom, 0):
            raise RuntimeError("custom preset configuration failed")
        app.history.original("synthetic", source, False)
        checks = {
            "raw": lambda s: s == source.lower(),
            "strict": lambda s: "alice" in s and "not" in s and ("7" in s or "seven" in s) and s.endswith("?"),
            "natural": lambda s: "alice" in s and "bob" in s and "friday" in s,
            "professional": lambda s: "alice" in s and "bob" in s and "friday" in s,
            "prompt": lambda s: "folder" in s and ("?" in s or "ready" in s),
            "translator": lambda s: "пап" in s and "не " in s and "пятниц" in s and "?" in s,
            "formatter": lambda s: "alice" in s and "bob" in s and "7" in s,
            "tasks": lambda s: "bob" in s and "friday" in s,
            "summary": lambda s: "bob" in s and "friday" in s,
            custom.id: lambda s: "alice" in s and "bob" in s and "folder" not in s,
        }
        failed = []
        if override_only:
            checks = {"translator": lambda s: "не " in s and "пап" in s and ("7" in s or "семь" in s)}
        for key, check in checks.items():
            window._open_entry("synthetic", "original")
            controls.once.setCurrentIndex(controls.once.findData(key))
            window._repeat()
            worker = app._worker
            if worker:
                worker.join(125 if override_only else 65)
                if worker.is_alive():
                    raise RuntimeError("worker did not finish")
            qt.processEvents()
            row = app.history.entry("synthetic")
            ok = (row["processing"] == "ready" and row["preset_id"] == key
                  and row["delivery"] == "not_requested" and check(row["text"].lower()))
            label = "custom" if key == custom.id else key
            print(f"{label} processing={row['processing']} delivery={row['delivery']} check={ok}", flush=True)
            if not ok:
                failed.append(label)
        saved = app.history.versions("synthetic")
        if failed:
            raise RuntimeError("Acceptance failed for " + ", ".join(failed))
        if saved["translator"]["model"] != instance:
            raise RuntimeError("override did not use selected instance")
    finally:
        app._shutdown()
    store = HistoryStore(path, profile)
    try:
        if store.versions("synthetic") != saved or store.entry("synthetic")["original"] != source:
            raise RuntimeError("restart mismatch")
        print(f"Restart verified results={len(saved)}", flush=True)
    finally:
        store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--override-only", action="store_true", help="Check Translator with no global model")
    args = parser.parse_args()
    with TemporaryDirectory(prefix="whisper-m2-") as directory:
        run(Path(directory), args.instance, args.override_only)
