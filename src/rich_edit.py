"""Small, early-bound subset of Windows' Rich Edit Text Object Model (tom.h).

The helper owns all interfaces in one COM apartment until the request ends.
"""
import ctypes as C
from ctypes import wintypes as W
from contextlib import ExitStack
import uuid

from win_target import bind

PTR = C.c_void_p
LONG = W.LONG


def checked(hr):
    if hr < 0:
        raise OSError("rich_edit_unavailable")


class Interface:
    def __init__(self, pointer, stack):
        if not pointer.value:
            raise OSError("missing_interface")
        self.pointer = pointer
        self.stack = stack
        stack.callback(self.call, 2, (), ())  # IUnknown::Release

    def call(self, slot, types=(), args=()):
        table = C.cast(self.pointer, C.POINTER(C.POINTER(PTR))).contents
        method = C.WINFUNCTYPE(LONG, PTR, *types)(table[slot])
        return method(self.pointer, *args)

    def interface(self, slot):
        pointer = PTR()
        checked(self.call(slot, (C.POINTER(PTR),), (C.byref(pointer),)))
        return Interface(pointer, self.stack)

    def number(self, slot):
        number = LONG()
        checked(self.call(slot, (C.POINTER(LONG),), (C.byref(number),)))
        return number.value


class RichRange:
    def __init__(self, stack: ExitStack, hwnd):
        ole = C.OleDLL("ole32")
        checked(ole.CoInitializeEx(None, 2))  # STA
        stack.callback(ole.CoUninitialize)
        access = bind(C.WinDLL("oleacc"), "AccessibleObjectFromWindow", LONG, W.HWND,
                      W.DWORD, PTR, C.POINTER(PTR))
        iid = uuid.UUID("8cc497c0-a1df-11ce-8098-00aa0047be5d").bytes_le
        pointer = PTR()
        checked(access(hwnd, 0xfffffff0, iid, C.byref(pointer)))  # OBJID_NATIVEOM, ITextDocument
        doc = Interface(pointer, stack)
        self.selection = doc.interface(8)  # ITextDocument::GetSelection
        self.range = self.selection.interface(11)  # ITextRange::GetDuplicate
        automation = C.WinDLL("oleaut32")
        self.free = bind(automation, "SysFreeString", None, PTR)
        self.allocate = bind(automation, "SysAllocString", PTR, W.LPCWSTR)
        self.length = bind(automation, "SysStringLen", W.UINT, PTR)

    def matches(self, start, end):
        # Compare ranges in the same live story. Equal text at another offset is insufficient.
        # Both interfaces came from GetSelection on this one document. TOM's
        # IsEqual can distinguish a selection from its duplicate at an empty caret.
        return (self.selection.number(14) == self.range.number(14) == start
                and self.selection.number(16) == self.range.number(16) == end
                and self.range.number(52) == -1)

    def text(self):
        string = PTR()
        checked(self.range.call(7, (C.POINTER(PTR),), (C.byref(string),)))
        try:
            return C.wstring_at(string, self.length(string)) if string else ""
        finally:
            self.free(string)

    def contains_result(self, start, text):
        # Check both UTF-16 endpoints before reading back from this range.
        end = start + len(text.encode("utf-16-le")) // 2
        return self.range.number(14) == start and self.range.number(16) == end and self.text() == text

    def replace(self, text):
        string = self.allocate(text)
        if not string:
            raise MemoryError
        try:
            if self.range.call(8, (PTR,), (string,)) != 0:  # ITextRange::SetText must return S_OK.
                raise OSError("rich_edit_write_unconfirmed")
        finally:
            self.free(string)
