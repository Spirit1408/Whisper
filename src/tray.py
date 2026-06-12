"""System tray icon with state indication and a context menu."""

from __future__ import annotations

import logging
from typing import Callable

import pystray
from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)

STATE_COLORS = {
    "idle": (100, 160, 220),       # blue
    "recording": (220, 60, 60),    # red
    "processing": (240, 170, 50),  # orange
}


def _make_icon(color: tuple[int, int, int], size: int = 64) -> Image.Image:
    """Draw a filled circle with a small mic-like rectangle."""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse([2, 2, size - 2, size - 2], fill=color + (255,))
    # simple mic glyph
    cx = size // 2
    draw.rounded_rectangle([cx - 7, 14, cx + 7, 36], radius=7, fill=(255, 255, 255, 255))
    draw.arc([cx - 13, 22, cx + 13, 44], start=0, end=180, fill=(255, 255, 255, 255), width=4)
    draw.line([cx, 44, cx, 52], fill=(255, 255, 255, 255), width=4)
    return image


class TrayApp:
    """Tray icon. Call run() on the main thread; it blocks until quit."""

    def __init__(
        self,
        postprocess_enabled: bool,
        on_toggle_postprocess: Callable[[bool], None],
        on_quit: Callable[[], None],
    ):
        self._postprocess_enabled = postprocess_enabled
        self._on_toggle_postprocess = on_toggle_postprocess
        self._on_quit = on_quit
        self._icons = {state: _make_icon(color) for state, color in STATE_COLORS.items()}

        menu = pystray.Menu(
            pystray.MenuItem(
                "LM Studio post-processing",
                self._toggle_postprocess,
                checked=lambda item: self._postprocess_enabled,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )
        self.icon = pystray.Icon("whisper-dictation", self._icons["idle"], "Dictation: idle", menu)

    def _toggle_postprocess(self, icon, item) -> None:
        self._postprocess_enabled = not self._postprocess_enabled
        self._on_toggle_postprocess(self._postprocess_enabled)

    def _quit(self, icon, item) -> None:
        self._on_quit()
        self.icon.stop()

    def set_state(self, state: str) -> None:
        """state: idle | recording | processing"""
        if state in self._icons:
            self.icon.icon = self._icons[state]
            self.icon.title = f"Dictation: {state}"

    def notify(self, message: str) -> None:
        try:
            self.icon.notify(message, "Dictation")
        except Exception:
            logger.debug("Tray notification failed", exc_info=True)

    def run(self) -> None:
        self.icon.run()
