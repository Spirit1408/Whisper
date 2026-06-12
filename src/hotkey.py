"""Global push-to-talk hotkey: callback on combo press, callback on release."""

from __future__ import annotations

import logging
from typing import Callable

import keyboard

logger = logging.getLogger(__name__)

# Canonical names for modifier key variants reported by the keyboard library
_KEY_ALIASES = {
    "left ctrl": "ctrl",
    "right ctrl": "ctrl",
    "left shift": "shift",
    "right shift": "shift",
    "left alt": "alt",
    "right alt": "alt",
    "alt gr": "alt",
    "left windows": "windows",
    "right windows": "windows",
    "win": "windows",
}


def _canonical(name: str) -> str:
    name = name.lower().strip()
    return _KEY_ALIASES.get(name, name)


class PushToTalkHotkey:
    """Tracks a key combo; fires on_press when fully held, on_release when broken."""

    def __init__(self, hotkey: str, on_press: Callable[[], None], on_release: Callable[[], None]):
        self.combo = {_canonical(part) for part in hotkey.split("+")}
        self.on_press = on_press
        self.on_release = on_release
        self._pressed: set[str] = set()
        self._active = False
        self._hook = None

    def start(self) -> None:
        self._hook = keyboard.hook(self._handle_event)
        logger.info("Push-to-talk hotkey active: %s", "+".join(sorted(self.combo)))

    def stop(self) -> None:
        if self._hook is not None:
            keyboard.unhook(self._hook)
            self._hook = None

    def _handle_event(self, event: keyboard.KeyboardEvent) -> None:
        if event.name is None:
            return
        key = _canonical(event.name)
        if key not in self.combo:
            return

        if event.event_type == keyboard.KEY_DOWN:
            self._pressed.add(key)
            if not self._active and self._pressed == self.combo:
                self._active = True
                try:
                    self.on_press()
                except Exception:
                    logger.exception("on_press callback failed")
        elif event.event_type == keyboard.KEY_UP:
            self._pressed.discard(key)
            if self._active:
                self._active = False
                try:
                    self.on_release()
                except Exception:
                    logger.exception("on_release callback failed")
