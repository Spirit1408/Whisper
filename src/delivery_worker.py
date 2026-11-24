"""Single-request Windows helper. Anonymous pipes only. stdout carries status codes."""
import ctypes as C
from ctypes import wintypes as W
from contextlib import ExitStack
import json
import sys

from rich_edit import RichRange
from delivery import plain
from win_target import Target, WindowsTarget, bind


class Changes:
    def __init__(self, stack, user, target):
        self.changed = False
        callback_type = C.WINFUNCTYPE(None, W.HANDLE, W.DWORD, W.HWND, W.LONG, W.LONG, W.DWORD, W.DWORD)

        @callback_type
        def on_event(hook, event, hwnd, obj, child, thread, timestamp):
            if event == 3:  # EVENT_SYSTEM_FOREGROUND
                self.changed |= hwnd != target.window
            elif event == 0x8005:  # EVENT_OBJECT_FOCUS
                self.changed |= hwnd != target.field or child != 0
            elif hwnd in (target.field, target.window):
                self.changed = True

        install = bind(user, "SetWinEventHook", W.HANDLE, W.DWORD, W.DWORD, W.HMODULE,
                       callback_type, W.DWORD, W.DWORD, W.DWORD)
        remove = bind(user, "UnhookWinEvent", W.BOOL, W.HANDLE)
        self.callback = on_event
        # Queued out-of-context notifications include focus-away-and-back and
        # selection/value changes even when the final offsets happen to match.
        for event in (3, 0x8005, 0x8001, 0x800e, 0x8014):
            pid = 0 if event == 3 else target.pid
            hook = install(event, event, None, on_event, pid, 0, 0)
            if not hook:
                raise OSError("event_hook_unavailable")
            stack.callback(remove, hook)
        self.peek = bind(user, "PeekMessageW", W.BOOL, C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT, W.UINT)
        self.dispatch = bind(user, "DispatchMessageW", W.LPARAM, C.POINTER(W.MSG))

    def unchanged(self):
        msg = W.MSG()
        while self.peek(C.byref(msg), None, 0, 0, 1):
            self.dispatch(C.byref(msg))
        return not self.changed


class PinnedTarget:
    def __init__(self, stack, native, target):
        self.native, self.target = native, target
        if native.capture() != target or target.end - target.start > 1024:
            raise ValueError("target_unavailable")
        self.changes = Changes(stack, native.user, target)
        self.span = RichRange(stack, target.field)
        if not self.span.matches(target.start, target.end):
            raise ValueError("range_unavailable")
        self.selected = ""
        if target.end != target.start:
            self.selected = self.span.text()
            if not plain(self.selected) or len(self.selected.encode("utf-16-le")) // 2 != target.end - target.start:
                raise ValueError("unsupported_selection")

    def valid(self):
        t = self.target
        return (self.native.capture() == t and self.span.matches(t.start, t.end)
                and (t.start == t.end or self.span.text() == self.selected)
                and self.changes.unchanged() and self.native.foreground() == t.window
                and self.native.sequence() == t.clipboard_sequence)

    def deliver(self, text):
        if not plain(text) or not self.valid():
            return "skipped"
        # The duplicate addresses the original story/range even if focus races
        # this call. Never select it, send keyboard input, or use the clipboard.
        self.span.replace(text)
        return "confirmed" if self.span.contains_result(self.target.start, text) else "paste_dispatched"


def respond(status):
    print(status, flush=True)


def serve():
    status = "skipped"
    try:
        with ExitStack() as stack:
            packet = json.loads(sys.stdin.buffer.readline())
            native = WindowsTarget()
            target = Target(**packet["target"])
            pinned = PinnedTarget(stack, native, target)
            if not pinned.valid() or native.input_tick() != packet["input_tick"]:
                return
            respond("ready")
            for line in sys.stdin.buffer:
                packet = json.loads(line)
                if packet == "verify":
                    respond("ready" if pinned.valid() else "skipped")
                elif isinstance(packet, dict) and "text" in packet:
                    status = "unknown"  # An exception can follow a completed COM call.
                    status = pinned.deliver(packet["text"])
                    return
                else:
                    return
    except Exception:
        pass  # COM exceptions can contain document data.
    finally:
        respond(status)


if __name__ == "__main__":
    serve()
