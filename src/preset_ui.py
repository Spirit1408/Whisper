"""Preset controls inside the existing dictation window."""
from dataclasses import replace
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
import threading

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QLineEdit, QPlainTextEdit, QPushButton, QSpinBox,
                               QVBoxLayout, QWidget)

from history import StorageError
from presets import BUILTINS, MAX_FILE, InvalidPreset, export_custom, import_custom, new_custom


def local_file(filename):
    path = Path(filename)
    if os.name == "nt":
        from ctypes import windll
        # Reject UNC, device namespaces and mapped network drives before opening
        # the file. Check the resolved volume too for local junctions.
        for resolve in (False, True):
            if resolve:
                path = path.resolve()
            if (not path.is_absolute() or len(path.drive) != 2 or path.is_reserved()
                    or any(":" in part for part in path.parts[1:])
                    or windll.kernel32.GetDriveTypeW(path.anchor) not in (2, 3, 6)):
                raise InvalidPreset("local_file_required")
    return path


def write_new_file(path, data):
    tmp = NamedTemporaryFile(dir=path.parent, prefix=".dictation-preset-", suffix=".tmp", delete=False)
    staging = Path(tmp.name)
    try:
        with tmp:
            tmp.write(data)
        # On Windows rename fails when the target exists. link has the same
        # create-only guarantee on POSIX, where rename would overwrite it.
        if os.name == "nt":
            staging.rename(path)
        else:
            os.link(staging, path)
    finally:
        staging.unlink(missing_ok=True)


class PresetSelection:
    """Only plain Python state crosses the Qt/hotkey thread boundary."""
    def __init__(self, history, settings):
        self.lock = threading.RLock()
        try:
            self.definitions = history.presets()
        except StorageError:
            self.definitions = dict(BUILTINS)
        self.active = settings.get("active_preset", "strict" if settings.get("lm_studio") else "raw")
        self.once = None


class PresetControls(QWidget):
    changed = Signal()
    notice = Signal(str)

    def __init__(self, history, settings, selection):
        super().__init__()
        self.history, self.settings, self.selection = history, settings, selection
        layout = QFormLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.active = QComboBox()
        self.once = QComboBox()
        layout.addRow("Active preset", self.active)
        layout.addRow("Next operation", self.once)
        buttons = QHBoxLayout()
        for title, action in (("Configure", self.configure), ("New", self.create),
                              ("Import JSON", self.read_file), ("Export JSON", self.write_file)):
            button = QPushButton(title)
            button.clicked.connect(action)
            buttons.addWidget(button)
        layout.addRow(buttons)
        self.refresh()
        self.active.currentIndexChanged.connect(self._set_active)
        self.once.currentIndexChanged.connect(self._set_once)

    def refresh(self):
        state = self.selection
        with state.lock:
            for combo, selected in ((self.active, state.active), (self.once, state.once)):
                combo.blockSignals(True)
                combo.clear()
                if combo is self.once:
                    combo.addItem("Use active preset", None)
                for preset in state.definitions.values():
                    combo.addItem(preset.label, preset.id)
                if selected and combo.findData(selected) < 0:
                    combo.addItem("Unavailable preset", selected)
                combo.setCurrentIndex(combo.findData(selected) if selected else 0)
                combo.blockSignals(False)

    def _set_active(self):
        key = self.active.currentData()
        with self.selection.lock:
            self.selection.active = key
            saved = self.settings.set("active_preset", key)
        self.changed.emit()
        if not saved:
            self.notice.emit("Preset selected for this session. Preference could not be saved.")

    def _set_once(self):
        with self.selection.lock:
            self.selection.once = self.once.currentData()

    def _save(self, preset, expected):
        try:
            saved = self.history.save_preset(preset, expected)
        except (InvalidPreset, StorageError):
            self.notice.emit("Preset could not be saved. Check its fields and reopen the definition.")
            return False
        with self.selection.lock:
            self.selection.definitions[saved.id] = saved
        self.refresh()
        self.notice.emit("Preset saved.")
        return True

    def configure(self):
        preset = self.selection.definitions.get(self.active.currentData())
        if preset and preset.id != "raw":
            self._edit(preset, preset.revision)
        else:
            self.notice.emit("Raw uses the source text directly.")

    def create(self):
        self._edit(new_custom(), 0)

    def _edit(self, preset, expected):
        dialog = QDialog(self)
        dialog.setWindowTitle("Configure preset")
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        layout.addLayout(form)
        name = QLineEdit(preset.name)
        name.setMaxLength(80)
        name.setReadOnly(not preset.custom)
        form.addRow("Name", name)
        instruction = QPlainTextEdit(preset.instruction)
        if preset.custom:
            form.addRow("Transformation", instruction)
        model = QLineEdit(preset.model)
        model.setPlaceholderText("Use global model")
        form.addRow("Loaded model key", model)
        timeout = QSpinBox()
        timeout.setRange(0, 300)
        timeout.setSpecialValueText("Use global timeout")
        timeout.setValue(preset.timeout)
        form.addRow("Timeout (seconds)", timeout)
        language = QLineEdit(preset.language)
        if preset.id == "translator":
            form.addRow("Target language", language)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        layout.addWidget(buttons)
        def save():
            updated = replace(preset, name=name.text(), instruction=instruction.toPlainText(),
                              model=model.text(), timeout=timeout.value(), language=language.text())
            if self._save(updated, expected):
                dialog.accept()
            else:
                dialog.setWindowTitle("Invalid or stale preset — check fields")
        buttons.accepted.connect(save)
        buttons.rejected.connect(dialog.reject)
        dialog.exec()

    def read_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import custom preset", "", "Presets (*.json)")
        if not path:
            return
        try:
            with local_file(path).open("rb") as stream:
                preset = import_custom(stream.read(MAX_FILE + 1))
            self._save(preset, 0)
        except (OSError, InvalidPreset):
            self.notice.emit("Import rejected. Expected a valid version 1 custom preset.")

    def write_file(self):
        preset = self.selection.definitions.get(self.active.currentData())
        if not preset or not preset.custom:
            self.notice.emit("Select a custom preset to export.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export custom preset to a new file", "", "Presets (*.json)")
        if path:
            try:
                data = export_custom(preset)
                write_new_file(local_file(path), data)
                self.notice.emit("Custom preset exported.")
            except (OSError, InvalidPreset):
                self.notice.emit("Export failed. Choose a new writable file.")
