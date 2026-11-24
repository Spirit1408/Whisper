"""The two Windows document controls eligible for a TOM range. No clipboard reads."""
import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass
import os
from pathlib import PureWindowsPath
import re


@dataclass(frozen=True)
class Target:
    pid: int
    thread: int
    window: int
    field: int
    start: int
    end: int
    clipboard_sequence: int
    process_start: int
    field_thread: int


def document_profile(image, window_class, field_class):
    path = PureWindowsPath(image)
    roots = {PureWindowsPath(p) for key in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")
             if (p := os.environ.get(key))}
    for root in roots:
        if not path.is_relative_to(root):
            continue
        relative = path.relative_to(root).as_posix().lower()
        if (relative == "windows nt/accessories/wordpad.exe"
                and window_class == "WordPadClass" and field_class.upper() == "RICHEDIT50W"):
            return True
        if (re.fullmatch(r"windowsapps/microsoft\.windowsnotepad_[\d.]+_(?:x64|x86|arm64)__8wekyb3d8bbwe/notepad/notepad\.exe", relative)
                and window_class == "Notepad" and field_class == "RichEditD2DPT"):
            return True
    return False


class GuiThreadInfo(C.Structure):
    _fields_ = [("cbSize", W.DWORD), ("flags", W.DWORD)] + [
        (name, W.HWND) for name in ("active", "focus", "capture", "menu", "move", "caret")
    ] + [("caret_rect", W.RECT)]


class LastInput(C.Structure):
    _fields_ = [("size", W.UINT), ("tick", W.DWORD)]


def bind(dll, name, result, *args):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = result, args
    return fn


class WindowsTarget:
    def __init__(self):
        self.user = C.WinDLL("user32")
        kernel = C.WinDLL("kernel32")
        self.foreground = bind(self.user, "GetForegroundWindow", W.HWND)
        self.thread = bind(self.user, "GetWindowThreadProcessId", W.DWORD, W.HWND, C.POINTER(W.DWORD))
        self.gui = bind(self.user, "GetGUIThreadInfo", W.BOOL, W.DWORD, C.POINTER(GuiThreadInfo))
        self.root = bind(self.user, "GetAncestor", W.HWND, W.HWND, W.UINT)
        self.class_name = bind(self.user, "GetClassNameW", C.c_int, W.HWND, W.LPWSTR, C.c_int)
        self.style = bind(self.user, "GetWindowLongW", W.LONG, W.HWND, C.c_int)
        self.visible = bind(self.user, "IsWindowVisible", W.BOOL, W.HWND)
        self.enabled = bind(self.user, "IsWindowEnabled", W.BOOL, W.HWND)
        self.sequence = bind(self.user, "GetClipboardSequenceNumber", W.DWORD)
        self.last_input = bind(self.user, "GetLastInputInfo", W.BOOL, C.POINTER(LastInput))
        self.send = bind(self.user, "SendMessageTimeoutW", W.LPARAM, W.HWND, W.UINT, W.WPARAM,
                         W.LPARAM, W.UINT, W.UINT, C.POINTER(C.c_size_t))
        self.open_process = bind(kernel, "OpenProcess", W.HANDLE, W.DWORD, W.BOOL, W.DWORD)
        self.close = bind(kernel, "CloseHandle", W.BOOL, W.HANDLE)
        self.image = bind(kernel, "QueryFullProcessImageNameW", W.BOOL, W.HANDLE, W.DWORD,
                          W.LPWSTR, C.POINTER(W.DWORD))
        self.times = bind(kernel, "GetProcessTimes", W.BOOL, W.HANDLE, *[C.POINTER(W.FILETIME)] * 4)
        adv = C.WinDLL("advapi32")
        self.open_token = bind(adv, "OpenProcessToken", W.BOOL, W.HANDLE, W.DWORD, C.POINTER(W.HANDLE))
        self.token_info = bind(adv, "GetTokenInformation", W.BOOL, W.HANDLE, C.c_int, C.c_void_p,
                               W.DWORD, C.POINTER(W.DWORD))

    def _class(self, hwnd):
        buf = C.create_unicode_buffer(256)
        return buf.value if self.class_name(hwnd, buf, len(buf)) else ""

    def input_tick(self):
        event = LastInput(size=C.sizeof(LastInput))
        return event.tick if self.last_input(C.byref(event)) else None

    def _process(self, pid):
        process = self.open_process(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not process:
            return "", 0
        token = W.HANDLE()
        try:
            if not self.open_token(process, 8, C.byref(token)):  # TOKEN_QUERY
                return "", 0
            elevated, used = W.DWORD(), W.DWORD()
            if not self.token_info(token, 20, C.byref(elevated), 4, C.byref(used)) or elevated.value:
                return "", 0
            buf = C.create_unicode_buffer(32768)
            size = W.DWORD(len(buf))
            times = [W.FILETIME() for _ in range(4)]
            if not self.image(process, 0, buf, C.byref(size)) or not self.times(process, *map(C.byref, times)):
                return "", 0
            created = times[0]
            return buf.value, (created.dwHighDateTime << 32) | created.dwLowDateTime
        finally:
            if token:
                self.close(token)
            self.close(process)

    def _focused_field(self, thread, window):
        info = GuiThreadInfo(cbSize=C.sizeof(GuiThreadInfo))
        if not thread or not self.gui(thread, C.byref(info)) or info.flags & ~1 or info.active != window:
            return None
        return info.focus

    def capture(self, excluded=()):
        window = self.foreground()
        pid = W.DWORD()
        thread = self.thread(window, C.byref(pid)) if window else 0
        field = self._focused_field(thread, window)
        if not field or self.root(field, 2) != window:
            return None
        field_pid = W.DWORD()
        field_thread = self.thread(field, C.byref(field_pid))
        if not field_thread or field_pid.value != pid.value or not self.visible(field) or not self.enabled(field):
            return None
        image, created = self._process(pid.value)
        if not image or PureWindowsPath(image).name.lower() in excluded:
            return None
        field_class = self._class(field)
        if not document_profile(image, self._class(window), field_class):
            return None
        # Packaged Notepad hosts its document on a second UI thread. Both
        # input queues must identify this exact active window and focused child.
        if field_thread != thread:
            if field_class != "RichEditD2DPT" or self._focused_field(field_thread, window) != field:
                return None
        style = self.style(field, -16)
        if not style & 4 or style & (0x20 | 0x800):  # Require ES_MULTILINE, reject PASSWORD/READONLY.
            return None
        # A control can set a password character independently of its initial style.
        result = C.c_size_t()
        if not self.send(field, 0xD2, 0, 0, 0x22, 80, C.byref(result)) or result.value:
            return None
        # EM_GETSEL is a system-marshalled message. Use full DWORD offsets.
        start, end = W.DWORD(0xffffffff), W.DWORD(0xffffffff)
        if not self.send(field, 0xB0, C.addressof(start), C.addressof(end), 0x22, 80, C.byref(result)):
            return None
        sequence = self.sequence()
        if start.value > end.value or end.value >= 0x7fffffff or not sequence or self.foreground() != window:
            return None
        return Target(pid.value, thread, window, field, start.value, end.value, sequence, created, field_thread)
