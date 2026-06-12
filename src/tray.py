"""System tray icon (Qt) with state indication and a minimal context menu."""

from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QAction, QBrush, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

logger = logging.getLogger(__name__)

STATE_COLORS = {
    "idle": QColor(100, 160, 220),       # blue
    "recording": QColor(220, 60, 60),    # red
    "processing": QColor(240, 170, 50),  # orange
}


def _make_icon(color: QColor, size: int = 64) -> QIcon:
    """Draw a filled circle with a small mic glyph."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QBrush(color))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(2, 2, size - 4, size - 4)

    white = QColor(255, 255, 255)
    painter.setBrush(QBrush(white))
    cx = size // 2
    painter.drawRoundedRect(QRect(cx - 7, 14, 14, 22), 7, 7)
    pen = QPen(white, 4)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawArc(QRect(cx - 13, 22, 26, 22), 180 * 16, 180 * 16)
    painter.drawLine(QPoint(cx, 44), QPoint(cx, 52))
    painter.end()
    return QIcon(pixmap)


class TrayIcon(QSystemTrayIcon):
    """Tray icon: double-click shows the window, menu has Show / Quit."""

    def __init__(self, on_show: Callable[[], None], on_quit: Callable[[], None]):
        self._icons = {state: _make_icon(color) for state, color in STATE_COLORS.items()}
        super().__init__(self._icons["idle"])
        self.setToolTip("Dictation: idle")

        menu = QMenu()
        show_action = QAction("Show window", menu)
        show_action.triggered.connect(on_show)
        menu.addAction(show_action)
        menu.addSeparator()
        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(on_quit)
        menu.addAction(quit_action)
        self.setContextMenu(menu)

        self._on_show = on_show
        self.activated.connect(self._on_activated)

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.DoubleClick:
            self._on_show()

    def set_state(self, state: str) -> None:
        """state: idle | recording | processing"""
        if state in self._icons:
            self.setIcon(self._icons[state])
            self.setToolTip(f"Dictation: {state}")

    def notify(self, message: str) -> None:
        self.showMessage("Dictation", message, QSystemTrayIcon.Information, 3000)
