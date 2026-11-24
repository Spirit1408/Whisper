"""A short-lived addressable delivery attempt. No clipboard or keyboard transport."""
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
import threading


def plain(text):
    # Rich content, embedded objects and control characters require their own policy.
    return isinstance(text, str) and 0 < len(text) <= 16384 and text.isprintable() and "\ufffc" not in text


class WindowsDelivery:
    def __init__(self, excluded):
        self.excluded = excluded

    def capture(self):
        if sys.platform != "win32" or self.excluded is None:
            return None
        from win_target import WindowsTarget
        native = WindowsTarget()
        tick = native.input_tick()
        target = native.capture(self.excluded)
        if target is None or tick is None:
            return None
        helper = subprocess.Popen(
            [sys.executable, "-B", "-u", str(Path(__file__).with_name("delivery_worker.py"))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        attempt = Attempt(helper)
        try:
            if attempt.exchange({"target": asdict(target), "input_tick": tick}) == "ready":
                return attempt
        except BaseException:
            attempt.close()
            raise
        attempt.close()
        return None


class Attempt:
    def __init__(self, process):
        self.process = process

    def exchange(self, packet):
        # COM providers and even pipe writes can stall. The helper has no
        # autonomous send path and each request has a bounded response time.
        response = []

        def request():
            try:
                self.process.stdin.write(json.dumps(packet).encode("ascii") + b"\n")
                self.process.stdin.flush()
                response.append(self.process.stdout.readline(40).strip().decode("ascii"))
            except (OSError, ValueError):
                pass

        io = threading.Thread(target=request, daemon=True)
        io.start()
        io.join(1.5)
        if io.is_alive():
            self.process.kill()
        return response[0] if response else None

    def verify(self):
        return self.exchange("verify") == "ready"

    def deliver(self, text):
        if not plain(text):
            return "skipped"
        result = self.exchange({"text": text})
        return result if result in ("confirmed", "paste_dispatched", "skipped") else "unknown"

    def close(self):
        try:
            self.process.stdin.close()  # EOF lets a healthy STA release its hooks and interfaces.
        except OSError:
            pass  # The timed-out helper may already have closed its end of the pipe.
        try:
            self.process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        finally:
            self.process.stdout.close()
