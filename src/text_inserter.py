"""Insert text into the active window via clipboard + Ctrl+V."""

from __future__ import annotations

import logging
import time

import keyboard
import pyperclip

logger = logging.getLogger(__name__)


class TextInserter:
    """Pastes text into the focused window, preserving the user's clipboard."""

    PASTE_DELAY = 0.15  # seconds to let the target app process the paste

    def insert(self, text: str) -> None:
        if not text:
            return
        previous_clipboard: str | None = None
        try:
            previous_clipboard = pyperclip.paste()
        except pyperclip.PyperclipException as e:
            logger.warning("Could not read clipboard for backup: %s", e)

        try:
            pyperclip.copy(text)
            time.sleep(0.05)
            keyboard.send("ctrl+v")
            time.sleep(self.PASTE_DELAY)
            logger.info("Inserted %d chars into active window", len(text))
        except Exception as e:
            logger.error("Failed to insert text: %s", e)
        finally:
            if previous_clipboard is not None:
                try:
                    pyperclip.copy(previous_clipboard)
                except pyperclip.PyperclipException as e:
                    logger.warning("Could not restore clipboard: %s", e)
