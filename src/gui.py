"""Main window: settings checkboxes, editable text field, history cards."""

from __future__ import annotations

import logging
from datetime import datetime

from PySide6.QtCore import QObject, Qt, Signal
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
    QVBoxLayout,
    QWidget,
)

import autostart
from audio_recorder import list_input_devices
from history import HistoryStore
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

    transcribed = Signal(str)
    state_changed = Signal(str)  # idle | recording | processing
    lm_status = Signal(bool)


class HistoryCard(QFrame):
    """A history entry card with a delete button shown on hover."""

    clicked = Signal(str)   # entry text
    deleted = Signal(str)   # entry id

    def __init__(self, entry: dict):
        super().__init__()
        self.setObjectName("card")
        self._entry = entry
        self.setCursor(Qt.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        header = QHBoxLayout()
        time_label = QLabel(datetime.fromtimestamp(entry["ts"]).strftime("%d.%m %H:%M"))
        time_label.setObjectName("cardTime")
        header.addWidget(time_label)
        header.addStretch()
        layout.addLayout(header)

        text_label = QLabel(entry["text"])
        text_label.setWordWrap(True)
        text_label.setTextInteractionFlags(Qt.NoTextInteraction)
        layout.addWidget(text_label)

        self._delete_btn = QPushButton("\u2715", self)
        self._delete_btn.setObjectName("cardDelete")
        self._delete_btn.setVisible(False)
        self._delete_btn.setCursor(Qt.PointingHandCursor)
        self._delete_btn.setFixedSize(20, 20)
        self._delete_btn.clicked.connect(lambda: self.deleted.emit(self._entry["id"]))
        self._delete_btn.raise_()
        self._position_delete_button()

    def enterEvent(self, event) -> None:
        self._delete_btn.setVisible(True)
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
    lm_recheck_requested = Signal()
    device_changed = Signal(object)  # device name (str) or None for system default

    def __init__(self, settings: SettingsStore, history: HistoryStore):
        super().__init__()
        self.settings = settings
        self.history = history
        self._cards: dict[str, HistoryCard] = {}

        self.setWindowTitle("Whisper Dictation")
        self.resize(440, 620)
        self.setStyleSheet(DARK_QSS)
        self._build_ui()
        self._populate_history()

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
        self.lm_cb.setEnabled(False)  # enabled after a successful health check
        self.lm_cb.toggled.connect(self._on_lm_toggled)
        lm_row.addWidget(self.lm_cb)

        self.lm_recheck_btn = QPushButton("\u27f3")
        self.lm_recheck_btn.setObjectName("recheck")
        self.lm_recheck_btn.setFixedWidth(30)
        self.lm_recheck_btn.setToolTip("Re-check LM Studio server")
        self.lm_recheck_btn.clicked.connect(self._on_recheck_clicked)
        lm_row.addWidget(self.lm_recheck_btn)

        self.lm_status_label = QLabel("checking\u2026")
        self.lm_status_label.setObjectName("status")
        lm_row.addWidget(self.lm_status_label)
        lm_row.addStretch()
        root.addLayout(lm_row)

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

        # Copy / Clear buttons
        btn_row = QHBoxLayout()
        copy_btn = QPushButton("Copy")
        copy_btn.clicked.connect(self._on_copy)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self.text_edit.clear)
        btn_row.addWidget(copy_btn)
        btn_row.addWidget(clear_btn)
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

    def _populate_history(self) -> None:
        for entry in self.history.entries:
            self._insert_card(entry, prepend=False)

    def _insert_card(self, entry: dict, prepend: bool = True) -> None:
        card = HistoryCard(entry)
        card.clicked.connect(self._on_card_clicked)
        card.deleted.connect(self._on_card_deleted)
        self._cards[entry["id"]] = card
        index = 0 if prepend else self.cards_layout.count() - 1  # keep stretch last
        self.cards_layout.insertWidget(index, card)

    def add_transcription(self, text: str) -> None:
        """Called (via signal) when a new dictation is transcribed."""
        entry = self.history.add(text)
        self._insert_card(entry, prepend=True)
        # Trim widgets beyond the store cap
        valid_ids = {e["id"] for e in self.history.entries}
        for entry_id in list(self._cards):
            if entry_id not in valid_ids:
                self._remove_card_widget(entry_id)

    def _remove_card_widget(self, entry_id: str) -> None:
        card = self._cards.pop(entry_id, None)
        if card is not None:
            self.cards_layout.removeWidget(card)
            card.deleteLater()

    def _on_card_clicked(self, text: str) -> None:
        self.text_edit.setPlainText(text)

    def _on_card_deleted(self, entry_id: str) -> None:
        self.history.remove(entry_id)
        self._remove_card_widget(entry_id)

    # --- checkboxes & buttons ----------------------------------------------

    def _on_autostart_toggled(self, checked: bool) -> None:
        if not autostart.set_enabled(checked):
            self.autostart_cb.blockSignals(True)
            self.autostart_cb.setChecked(not checked)
            self.autostart_cb.blockSignals(False)

    def _on_lm_toggled(self, checked: bool) -> None:
        self.settings.set("lm_studio", checked)
        self.lm_toggle_requested.emit(checked)

    def _on_recheck_clicked(self) -> None:
        self.lm_status_label.setText("checking\u2026")
        self.lm_recheck_requested.emit()

    def set_lm_available(self, available: bool) -> None:
        """Update LM Studio checkbox according to server availability."""
        self.lm_cb.blockSignals(True)
        if available:
            self.lm_cb.setEnabled(True)
            self.lm_cb.setChecked(bool(self.settings.get("lm_studio", False)))
            self.lm_status_label.setText("server online")
        else:
            self.lm_cb.setChecked(False)
            self.lm_cb.setEnabled(False)
            self.lm_status_label.setText("server offline")
        self.lm_cb.blockSignals(False)
        # Apply the effective state to the pipeline
        self.lm_toggle_requested.emit(available and bool(self.settings.get("lm_studio", False)))

    def _on_copy(self) -> None:
        text = self.text_edit.toPlainText()
        if text:
            QGuiApplication.clipboard().setText(text)

    # --- window behavior ----------------------------------------------------

    def closeEvent(self, event) -> None:
        """Hide to tray instead of quitting."""
        if QApplication.instance().property("really_quit"):
            event.accept()
            return
        event.ignore()
        self.hide()

    def show_and_raise(self) -> None:
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.raise_()
        self.activateWindow()
