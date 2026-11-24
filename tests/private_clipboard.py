"""Exercise Qt's Windows clipboard only after verifying a new private window station."""
import ctypes as C
from ctypes import wintypes as W
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from win_target import bind


def run():
    user = C.WinDLL("user32", use_last_error=True)
    name = "whisper-test-" + uuid4().hex
    create = bind(user, "CreateWindowStationW", W.HANDLE, W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p)
    station = create(name, 0, 0x0f, None)  # Enumerate/read station, clipboard, create desktop.
    if not station:
        raise OSError(C.get_last_error(), "private_station_unavailable")
    if not bind(user, "SetProcessWindowStation", W.BOOL, W.HANDLE)(station):
        raise OSError(C.get_last_error(), "private_station_not_assigned")
    current = bind(user, "GetProcessWindowStation", W.HANDLE)()
    info = bind(user, "GetUserObjectInformationW", W.BOOL, W.HANDLE, C.c_int, C.c_void_p,
                W.DWORD, C.POINTER(W.DWORD))
    label, used = C.create_unicode_buffer(128), W.DWORD()
    if not info(current, 2, label, C.sizeof(label), C.byref(used)) or label.value != name:
        raise RuntimeError("private_station_not_verified")
    from rich_edit_host import private_desktop
    private_desktop(name, True)

    os.environ["QT_QPA_PLATFORM"] = "windows"
    from PySide6.QtCore import QMimeData
    from PySide6.QtWidgets import QApplication
    from gui import MainWindow
    qt = QApplication([])
    clip = qt.clipboard()
    image = QMimeData()
    image.setData("application/x-test-binary", b"\x01\x02")
    clip.setMimeData(image)
    if bytes(clip.mimeData().data("application/x-test-binary")) != b"\x01\x02":
        raise AssertionError("private_clipboard_setup")
    notices = []
    window = SimpleNamespace(text_edit=SimpleNamespace(toPlainText=lambda: "synthetic copy"),
                             _selected=None, show_notice=notices.append)
    MainWindow._on_copy(window)
    if notices != ["Copied to clipboard."] or clip.text() != "synthetic copy":
        raise AssertionError("private_copy_unconfirmed")
    # A subsequent owner is free to replace that explicit copy. No restore is scheduled.
    clip.setText("second synthetic copy")
    qt.processEvents()
    if clip.text() != "second synthetic copy":
        raise AssertionError("new_copy_lost")


if __name__ == "__main__":
    try:
        run()
    except OSError as exc:
        if exc.errno == 5 and exc.strerror == "private_station_unavailable":
            sys.exit(77)
        raise
