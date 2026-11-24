"""Main window: settings checkboxes, editable text field, history cards."""

from __future__ import annotations

import logging
from datetime import datetime

from PySide6.QtCore import QMimeData, QObject, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

import autostart
from audio_recorder import list_input_devices
from history import HistoryStore, StorageError
from postprocessor import SMART_ERRORS
from settings import SettingsStore

logger = logging.getLogger(__name__)

DARK_QSS = """
QWidget {
    background-color: #1e1e1e;
    color: #d4d4d4;
    font-size: 13px;
}
QCheckBox { spacing: 8px; }
QCheckBox::indicator {
    width: 16px; height: 16px;
    border: 1px solid #555; border-radius: 3px;
    background: #2a2a2a;
}
QCheckBox::indicator:checked { background: #4a90d9; border-color: #4a90d9; }
QCheckBox:disabled { color: #666; }
QPlainTextEdit {
    background-color: #252526;
    border: 1px solid #3c3c3c;
    border-radius: 6px;
    padding: 8px;
    selection-background-color: #264f78;
}
QPushButton {
    background-color: #2d2d2d;
    border: 1px solid #3c3c3c;
    border-radius: 6px;
    padding: 6px 18px;
}
QPushButton:hover { background-color: #3a3a3a; }
QPushButton:pressed { background-color: #4a90d9; }
QPushButton:disabled { color: #666; }
QScrollArea { border: none; }
QScrollBar:vertical {
    background: #1e1e1e; width: 10px; margin: 0;
}
QScrollBar::handle:vertical {
    background: #3c3c3c; border-radius: 5px; min-height: 24px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QFrame#card {
    background-color: #252526;
    border: 1px solid #3c3c3c;
    border-radius: 6px;
}
QFrame#card:hover { border-color: #4a90d9; }
QLabel#cardTime { color: #777; font-size: 11px; }
QPushButton#cardDelete {
    background: transparent; border: none;
    color: #888; font-size: 14px; padding: 0 6px;
}
QPushButton#cardDelete:hover { color: #e05555; }
QPushButton#recheck {
    padding: 2px 8px; font-size: 13px;
}
QLabel#status { color: #777; font-size: 11px; }
QComboBox {
    background-color: #2d2d2d;
    border: 1px solid #3c3c3c;
    border-radius: 6px;
    padding: 5px 10px;
}
QComboBox::drop-down { border: none; width: 24px; }
QComboBox QAbstractItemView {
    background-color: #2d2d2d;
    border: 1px solid #3c3c3c;
    selection-background-color: #264f78;
}
"""


class GuiBridge(QObject):
    """Thread-safe bridge: worker threads emit, GUI thread receives."""

    transcribed = Signal(object)
    notice = Signal(str)
    status_changed = Signal(str)  # idle | listening | processing
    lm_progress = Signal(str)          # transient status text shown next to the button
    lm_models = Signal(list, object)   # (available [(key, name)], loaded_key | None)
    lm_ready = Signal(bool, object)    # (model_loaded, loaded_key | None)
    lm_selected = Signal(str)


class HistoryCard(QFrame):
    """A history entry card with a delete button shown on hover."""

    clicked = Signal(str)   # entry text
    deleted = Signal(str)   # entry id

    @staticmethod
    def status_text(entry):
        stamp = datetime.fromtimestamp(entry["ts"]).strftime("%d.%m %H:%M") if entry["ts"] is not None else "Unknown date"
        if entry.get("ram"):
            stamp = "Not saved · RAM only"
        elif entry["original"] is None:
            stamp += " · Legacy · Original unknown"
        else:
            saved = "Original + Smart" if entry["storage"] == "result" else "Original"
            delivery = {"skipped": "paste skipped", "not_requested": "Artifact Only",
                        "copied": "copied", "confirmed": "delivery confirmed",
                        "dispatching": "delivery pending", "paste_dispatched": "paste dispatched",
                        "unknown": "delivery unknown"}.get(entry["delivery"], "manual delivery")
            stamp += f" · {entry['processing']} · {saved} · {delivery}"
            if entry["error"] == "result_write_failed":
                stamp += " · Smart result not saved"
            elif entry["error"] == "original_read_failed":
                stamp += " · Original read failed"
            elif entry["processing"] == "fallback":
                stamp += " · " + SMART_ERRORS.get(entry["error"], "Smart correction unavailable")
        return stamp

    def __init__(self, entry: dict):
        super().__init__()
        self.setObjectName("card")
        self._entry = entry
        self.setCursor(Qt.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        header = QHBoxLayout()
        self.time_label = QLabel(self.status_text(entry))
        self.time_label.setObjectName("cardTime")
        header.addWidget(self.time_label)
        header.addStretch()
        layout.addLayout(header)

        text_label = QLabel(entry["text"])
        text_label.setTextFormat(Qt.PlainText)
        text_label.setWordWrap(True)
        text_label.setTextInteractionFlags(Qt.NoTextInteraction)
        layout.addWidget(text_label)

        self._delete_btn = QPushButton("\u2715", self)
        self._delete_btn.setObjectName("cardDelete")
        self._delete_btn.setVisible(False)
        self._delete_btn.setCursor(Qt.PointingHandCursor)
        self._delete_btn.setFixedSize(20, 20)
        self._delete_btn.clicked.connect(lambda: self.deleted.emit(self._entry["id"]))
        if entry.get("ram"):
            self._delete_btn.setEnabled(False)
        if entry.get("original") is not None and entry["original"] != entry["text"]:
            original_btn = QPushButton("Open Original")
            original_btn.clicked.connect(lambda: self.clicked.emit(entry["original"]))
            layout.addWidget(original_btn)
        self._delete_btn.raise_()
        self._position_delete_button()

    def mark_copied(self):
        self._entry["delivery"] = "copied"
        self.time_label.setText(self.status_text(self._entry))

    def enterEvent(self, event) -> None:
        self._delete_btn.setVisible(self._delete_btn.isEnabled())
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._delete_btn.setVisible(False)
        super().leaveEvent(event)

    def resizeEvent(self, event) -> None:
        self._position_delete_button()
        super().resizeEvent(event)

    def _position_delete_button(self) -> None:
        self._delete_btn.move(self.width() - self._delete_btn.width() - 10, 8)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._entry["text"])
        super().mousePressEvent(event)


class MainWindow(QWidget):
    """Dark-themed main window. Closing hides to tray instead of quitting."""

    lm_toggle_requested = Signal(bool)
    lm_ensure_requested = Signal()       # refresh clicked: bring LM Studio to ready state
    lm_model_changed = Signal(str)       # user picked a model key in the dropdown
    device_changed = Signal(object)  # device name (str) or None for system default
    notification = Signal(str)
    cancel_requested = Signal()

    def __init__(self, settings: SettingsStore, history: HistoryStore, ram_results):
        super().__init__()
        self.settings = settings
        self.history = history
        self.ram_results = ram_results
        self._selected = None
        self._cursor = None
        self._cards: dict[str, HistoryCard] = {}

        self.setWindowTitle("Whisper Dictation")
        self.resize(440, 620)
        self.setStyleSheet(DARK_QSS)
        self._build_ui()
        self.refresh_history()

    # --- UI construction -------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        # Checkboxes
        self.autostart_cb = QCheckBox("Start with Windows")
        self.autostart_cb.setChecked(autostart.is_enabled())
        self.autostart_cb.toggled.connect(self._on_autostart_toggled)
        root.addWidget(self.autostart_cb)

        lm_row = QHBoxLayout()
        self.lm_cb = QCheckBox("LM Studio post-processing")
        self._lm_ready = False
        self.lm_cb.setEnabled(False)  # enabled after a successful health check
        self.lm_cb.toggled.connect(self._on_lm_toggled)
        lm_row.addWidget(self.lm_cb)

        self.lm_recheck_btn = QPushButton("\u27f3")
        self.lm_recheck_btn.setObjectName("recheck")
        self.lm_recheck_btn.setFixedWidth(30)
        self.lm_recheck_btn.setToolTip("Start LM Studio server and load a model")
        self.lm_recheck_btn.clicked.connect(self._on_ensure_clicked)
        lm_row.addWidget(self.lm_recheck_btn)

        self.lm_status_label = QLabel("checking\u2026")
        self.lm_status_label.setObjectName("status")
        lm_row.addWidget(self.lm_status_label)
        lm_row.addStretch()
        root.addLayout(lm_row)

        # LM Studio model selector (enabled once the server is ready)
        self.lm_model_combo = QComboBox()
        self.lm_model_combo.setToolTip("LM Studio model used for post-processing")
        self.lm_model_combo.setEnabled(False)
        self._lm_has_models = False
        self.lm_model_combo.addItem("No model loaded", None)
        self.lm_model_combo.currentIndexChanged.connect(self._on_lm_model_selected)
        root.addWidget(self.lm_model_combo)

        # Microphone selector
        mic_row = QHBoxLayout()
        self.mic_combo = QComboBox()
        self.mic_combo.setToolTip("Audio capture device")
        mic_row.addWidget(self.mic_combo, stretch=1)

        mic_refresh_btn = QPushButton("\u27f3")
        mic_refresh_btn.setObjectName("recheck")
        mic_refresh_btn.setFixedWidth(30)
        mic_refresh_btn.setToolTip("Refresh device list")
        mic_refresh_btn.clicked.connect(self._populate_devices)
        mic_row.addWidget(mic_refresh_btn)
        root.addLayout(mic_row)

        self._populate_devices()
        self.mic_combo.currentIndexChanged.connect(self._on_device_selected)

        # Editable text field
        self.text_edit = QPlainTextEdit()
        self.text_edit.setPlaceholderText("Transcribed text appears here for manual editing\u2026")
        self.text_edit.setMinimumHeight(120)
        root.addWidget(self.text_edit)
        delivery_row = QHBoxLayout()
        delivery_row.addWidget(QLabel("Delivery"))
        self.delivery_combo = QComboBox()
        self.delivery_combo.addItem("Artifact Only", "artifact")
        self.delivery_combo.addItem("Auto Paste · verified fields", "auto")
        self.delivery_combo.setCurrentIndex(1 if self.settings.get("delivery_mode") == "auto" else 0)
        self.delivery_combo.currentIndexChanged.connect(
            lambda _: self.settings.set("delivery_mode", self.delivery_combo.currentData()))
        delivery_row.addWidget(self.delivery_combo)
        root.addLayout(delivery_row)
        self.result_label = QLabel("Copy is available after dictation.")
        self.result_label.setWordWrap(True)
        root.addWidget(self.result_label)

        warnings = []
        for source, report in self.history.reports.items():
            if report.status not in ("imported", "already_imported", "absent") or report.rejected:
                warnings.append(f"{source}: {report.status}, skipped {report.rejected}")
        if warnings:
            migration_label = QLabel("Storage: " + " · ".join(warnings))
            migration_label.setWordWrap(True)
            root.addWidget(migration_label)

        # Copy / Clear buttons
        btn_row = QHBoxLayout()
        copy_btn = QPushButton("Copy")
        copy_btn.clicked.connect(self._on_copy)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self.text_edit.clear)
        btn_row.addWidget(copy_btn)
        btn_row.addWidget(clear_btn)
        cancel_btn = QPushButton("Cancel dictation")
        cancel_btn.clicked.connect(self.cancel_requested)
        btn_row.addWidget(cancel_btn)
        btn_row.addStretch()
        root.addLayout(btn_row)

        # History cards in a scroll area
        history_label = QLabel("History")
        history_label.setObjectName("status")
        root.addWidget(history_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        self.cards_layout = QVBoxLayout(container)
        self.cards_layout.setContentsMargins(0, 0, 4, 0)
        self.cards_layout.setSpacing(8)
        self.cards_layout.addStretch()
        scroll.setWidget(container)
        root.addWidget(scroll, stretch=1)

        self.more_btn = QPushButton("Older entries")
        self.more_btn.clicked.connect(self._load_page)
        root.addWidget(self.more_btn)

        # Status bar
        self.status_bar = QStatusBar()
        self.status_bar.setObjectName("status")
        self.status_bar.setFixedHeight(20)
        self.set_status("idle")
        root.addWidget(self.status_bar)

    # --- microphone selector ----------------------------------------------

    def _populate_devices(self) -> None:
        """Fill the combobox with input devices, keeping the saved selection."""
        saved = self.settings.get("audio_device")
        self.mic_combo.blockSignals(True)
        self.mic_combo.clear()
        self.mic_combo.addItem("System default microphone", None)
        for _idx, name in list_input_devices():
            self.mic_combo.addItem(name, name)
        if saved:
            pos = self.mic_combo.findData(saved)
            if pos >= 0:
                self.mic_combo.setCurrentIndex(pos)
        self.mic_combo.blockSignals(False)

    def _on_device_selected(self, _index: int) -> None:
        name = self.mic_combo.currentData()
        self.settings.set("audio_device", name)
        self.device_changed.emit(name)

    # --- history ----------------------------------------------------------

    def refresh_history(self):
        for key in list(self._cards):
            self._remove_card_widget(key)
        self._cursor = None
        for item in reversed(self.ram_results()):
            self._insert_card(dict(id="ram-" + item.id, text=item.text, original=None, ts=None, ram=True))
        self._load_page()

    def _load_page(self):
        try:
            entries = self.history.page(self._cursor, limit=51)
        except StorageError:
            self.show_notice("History unavailable. Use Copy for text in the editor.")
            entries = []
        for entry in entries[:50]:
            self._insert_card(entry)
            self._cursor = entry["cursor"]
        self.more_btn.setVisible(len(entries) > 50)

    def _insert_card(self, entry):
        card = HistoryCard(entry)
        card.clicked.connect(lambda text: self._select_text(entry, text))
        card.deleted.connect(self._on_card_deleted)
        self._cards[entry["id"]] = card
        self.cards_layout.insertWidget(self.cards_layout.count() - 1, card)

    def _select_text(self, entry, text):
        self._selected = (entry["id"], text) if not entry.get("ram") else None
        self.text_edit.setPlainText(text)

    def add_transcription(self, outcome):
        self._selected = (outcome.id, outcome.text) if outcome.error is None else None
        self.text_edit.setPlainText(outcome.text)
        self.refresh_history()
        self.show_notice(outcome.message)

    def _remove_card_widget(self, entry_id: str) -> None:
        card = self._cards.pop(entry_id, None)
        if card is not None:
            self.cards_layout.removeWidget(card)
            card.deleteLater()

    def _on_card_deleted(self, entry_id: str) -> None:
        try:
            self.history.remove(entry_id)
            self._remove_card_widget(entry_id)
        except StorageError:
            self.show_notice("History change could not be saved.")

    # --- checkboxes & buttons ----------------------------------------------

    def _on_autostart_toggled(self, checked: bool) -> None:
        if not autostart.set_enabled(checked):
            self.autostart_cb.blockSignals(True)
            self.autostart_cb.setChecked(not checked)
            self.autostart_cb.blockSignals(False)

    def _on_lm_toggled(self, checked: bool) -> None:
        self.settings.set("lm_studio", checked)
        self.lm_cb.setEnabled(checked or self._lm_ready)
        self.lm_toggle_requested.emit(checked)

    def _on_ensure_clicked(self) -> None:
        """Refresh clicked: start the server and load a model in the background."""
        self._set_lm_busy("checking...")
        self.lm_ensure_requested.emit()

    def _on_lm_model_selected(self, _index: int) -> None:
        """User picked a model in the dropdown - request a load."""
        key = self.lm_model_combo.currentData()
        if key is None:
            return
        self._set_lm_busy("loading...")
        self.lm_model_changed.emit(key)

    def _set_lm_busy(self, text: str) -> None:
        """Lock LM Studio controls while a background operation runs."""
        self.lm_recheck_btn.setEnabled(False)
        self.lm_model_combo.setEnabled(False)
        self._show_lm_choice(False)
        self.lm_status_label.setText(text)

    def set_lm_progress(self, text: str) -> None:
        """Show transient progress text emitted by the worker thread."""
        self.lm_status_label.setText(text)

    def set_lm_models(self, available: list, loaded_key) -> None:
        """Populate the model dropdown without triggering a load."""
        self._lm_has_models = bool(available)
        self.lm_model_combo.blockSignals(True)
        self.lm_model_combo.clear()
        if available:
            if loaded_key is None:
                self.lm_model_combo.addItem("Select a downloaded model", None)
            for key, name in available:
                self.lm_model_combo.addItem(name, key)
            pos = self.lm_model_combo.findData(loaded_key) if loaded_key else -1
            if pos >= 0:
                self.lm_model_combo.setCurrentIndex(pos)
        else:
            self.lm_model_combo.addItem("No models found", None)
        self.lm_model_combo.blockSignals(False)

    def remember_lm_model(self, key: str) -> None:
        if self.settings.get("lm_model") != key:
            self.settings.set("lm_model", key)

    def set_lm_ready(self, model_loaded: bool, loaded_key) -> None:
        """Re-enable controls and reflect the final readiness state."""
        self.lm_recheck_btn.setEnabled(True)
        self.lm_model_combo.setEnabled(self._lm_has_models)
        if loaded_key:
            self.lm_model_combo.blockSignals(True)
            pos = self.lm_model_combo.findData(loaded_key)
            if pos >= 0:
                self.lm_model_combo.setCurrentIndex(pos)
            self.lm_model_combo.blockSignals(False)
        if model_loaded:
            self.lm_status_label.setText("ready")
        self._show_lm_choice(model_loaded)

    def _show_lm_choice(self, ready):
        # Losing readiness cannot turn an opted-in Smart request into an automatic Raw paste.
        self._lm_ready = ready
        requested = bool(self.settings.get("lm_studio", False))
        self.lm_cb.blockSignals(True)
        self.lm_cb.setChecked(requested)
        self.lm_cb.setEnabled(ready or requested)
        self.lm_cb.blockSignals(False)
        self.lm_toggle_requested.emit(requested)

    def _on_copy(self) -> None:
        text = self.text_edit.toPlainText()
        if text:
            try:
                clipboard = QGuiApplication.clipboard()
                data = QMimeData()
                data.setText(text)
                clipboard.setMimeData(data)
                if not clipboard.ownsClipboard() or clipboard.mimeData() is not data:
                    self.show_notice("Copy could not be verified. The text is still in the editor.")
                    return
                if self._selected and self._selected[1] == text:
                    selected = self._selected[0]
                    if self.history.copied(selected) and selected in self._cards:
                        self._cards[selected].mark_copied()
                self.show_notice("Copied to clipboard.")
            except StorageError:
                self.show_notice("Copied to clipboard. Copy status could not be saved.")
            except (RuntimeError, OSError):
                self.show_notice("Copy failed. The result is available in the editor.")

    def show_notice(self, message: str) -> None:
        self.result_label.setText(message)
        if not self.isVisible() or self.isMinimized():
            self.notification.emit(message)

    # --- window behavior ----------------------------------------------------

    def closeEvent(self, event) -> None:
        """Hide to tray instead of quitting."""
        if QApplication.instance().property("really_quit"):
            event.accept()
            return
        event.ignore()
        self.hide()

    def set_status(self, status: str) -> None:
        """Update status bar text and color."""
        colors = {
            "idle": "#777",
            "listening": "#4a90d9",
            "processing": "#d9a34a",
        }
        labels = {
            "idle": "Idle",
            "listening": "Listening...",
            "processing": "Processing...",
        }
        color = colors.get(status, "#777")
        text = labels.get(status, status)
        self.status_bar.setStyleSheet(f'QStatusBar {{ color: {color}; }}')
        self.status_bar.showMessage(text)

    def show_and_raise(self) -> None:
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.raise_()
        self.activateWindow()
