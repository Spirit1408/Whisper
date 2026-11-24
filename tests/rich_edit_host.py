"""A separate message-pumping process with two hidden, synthetic Windows controls."""
import ctypes as C
from ctypes import wintypes as W
from pathlib import Path
import os
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from win_target import bind


def private_desktop(name, create):
    if not name.startswith("whisper-test-") or not name[13:].isalnum():
        raise ValueError("invalid_fixture_desktop")
    user = C.WinDLL("user32")
    if create:
        make = bind(user, "CreateDesktopW", W.HANDLE, W.LPCWSTR, W.LPCWSTR, C.c_void_p,
                    W.DWORD, W.DWORD, C.c_void_p)
        desktop = make(name, None, None, 0, 0x1ff, None)
    else:
        desktop = bind(user, "OpenDesktopW", W.HANDLE, W.LPCWSTR, W.DWORD, W.BOOL,
                       W.DWORD)(name, 0, False, 0x1ff)
    if not desktop or not bind(user, "SetThreadDesktop", W.BOOL, W.HANDLE)(desktop):
        raise OSError("private_desktop_unavailable")


def run():
    ole = C.OleDLL("ole32")
    ole.CoInitializeEx(None, 2)
    user = C.WinDLL("user32")
    field_class = "RICHEDIT50W"
    library = "msftedit"
    if "--notepad-provider" in sys.argv:
        library = sys.argv[sys.argv.index("--notepad-provider") + 1]
        field_class = "RichEditD2DPT"
    try:
        rich_edit = C.WinDLL(library)  # Keep the window class registered.
    except PermissionError:
        if "--notepad-provider" in sys.argv:
            sys.exit(77)
        raise
    create = bind(user, "CreateWindowExW", W.HWND, W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD,
                  C.c_int, C.c_int, C.c_int, C.c_int, W.HWND, W.HMENU, W.HINSTANCE, C.c_void_p)
    # The optional visible variant must belong to a separate, inactive test desktop.
    window, style = None, 0x80000004
    if "--private-desktop" in sys.argv:
        window = create(0, "STATIC", "", 0x10cf0000, 0, 0, 300, 200, None, None, None, None)
        style = 0x50000004
    fields = [create(0, field_class, "alpha alpha", style, 0, i * 55, 100, 50, window, None,
                     None, None) for i in range(2)]
    if not all(fields):
        raise RuntimeError("test_window_unavailable")
    if window:
        bind(user, "SetActiveWindow", W.HWND, W.HWND)(window)
        bind(user, "SetFocus", W.HWND, W.HWND)(fields[0])
        thread = C.WinDLL("kernel32").GetCurrentThreadId()
        post = bind(user, "PostThreadMessageW", W.BOOL, W.DWORD, W.UINT, W.WPARAM, W.LPARAM)
        def commands():
            for line in sys.stdin:
                if line.strip() == "move_focus_back":
                    post(thread, 0x8001, 0, 0)
        threading.Thread(target=commands, daemon=True).start()
        print(window, os.getpid(), thread, *fields, flush=True)
    else:
        print(*fields, flush=True)
    msg = W.MSG()
    while user.GetMessageW(C.byref(msg), None, 0, 0) > 0:
        if window and msg.message == 0x8001:
            user.SetFocus(fields[1])
            user.SetFocus(fields[0])
            print("done", flush=True)
            continue
        user.TranslateMessage(C.byref(msg))
        user.DispatchMessageW(C.byref(msg))


if __name__ == "__main__":
    if "--private-desktop" in sys.argv:
        private_desktop(sys.argv[sys.argv.index("--private-desktop") + 1], "--test-worker" not in sys.argv)
    if "--test-worker" in sys.argv:
        # Only this fixture's trusted process/controls stand in for a packaged editor.
        import delivery_worker
        import win_target
        hwnd, pid = map(int, sys.argv[-2:])
        class FixtureTarget(win_target.WindowsTarget):
            def __init__(self):
                super().__init__()
                self.foreground = lambda: hwnd  # An inactive desktop has no input foreground.
                self.sequence = lambda: 1       # No shared Windows clipboard access.
                self.input_tick = lambda: 8

            def _process(self, candidate):
                return ("fixture.exe", 1) if candidate == pid else ("", 0)

        win_target.document_profile = lambda path, root, field: (
            path == "fixture.exe" and root.upper() == "STATIC" and field.upper() == "RICHEDIT50W")
        delivery_worker.WindowsTarget = FixtureTarget
        delivery_worker.serve()
    else:
        run()
