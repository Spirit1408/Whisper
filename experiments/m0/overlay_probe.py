"""Observe a native Qt overlay without activating windows or inspecting their contents."""
import ctypes
from ctypes import wintypes as W
from PySide6.QtWidgets import QApplication, QProgressBar
from PySide6.QtCore import Qt, QTimer

user32 = ctypes.WinDLL("user32", use_last_error=True)


class ThreadInfo(ctypes.Structure):
    _fields_ = [("cbSize", W.DWORD), ("flags", W.DWORD),
                *((n, W.HWND) for n in ("active", "focus", "capture", "menu", "move", "caret")),
                ("rcCaret", W.RECT)]


def focus():
    info = ThreadInfo()
    info.cbSize = ctypes.sizeof(info)
    user32.GetForegroundWindow.restype = W.HWND
    fg = user32.GetForegroundWindow()
    if fg and user32.GetGUIThreadInfo(0, ctypes.byref(info)):
        return fg, info.focus
    return None, None


app = QApplication([])
label = QProgressBar()
label.setTextVisible(False)
label.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus | Qt.WindowTransparentForInput)
label.setAttribute(Qt.WA_ShowWithoutActivating)
label.move(-30000, -30000)
before = focus()
states = []


def show():
    label.show()
    for delay, level in ((20, 25), (60, 85), (100, 40)):
        QTimer.singleShot(delay, lambda value=level: update(value))
    QTimer.singleShot(150, hide)


def update(value):
    label.setValue(value)
    states.append(focus())


def hide():
    states.append(focus())
    label.hide()
    QTimer.singleShot(100, app.quit)


app.setQuitOnLastWindowClosed(False)
QTimer.singleShot(0, show)
app.exec()
states.append(focus())
print("platform", app.platformName(), "screens", len(app.screens()))
print("observable_foreground", bool(before[0]), "observable_focus", bool(before[1]),
      "unchanged", all(state == before for state in states))
