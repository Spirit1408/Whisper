"""Persistent UI state (checkbox values) stored in state.json."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from json_store import write_json

logger = logging.getLogger(__name__)

STATE_PATH = Path(__file__).resolve().parent.parent / "state.json"

DEFAULTS = {
    "autostart": False,
    "lm_studio": False,
}


class SettingsStore:
    """Tiny JSON-backed key-value store for UI state."""

    def __init__(self, path: Path = STATE_PATH):
        self.path = path
        self._data: dict = dict(DEFAULTS)
        self._writable = True
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                stored = json.load(f)
            if not isinstance(stored, dict):
                raise ValueError
            self._data.update(stored)
        except (ValueError, OSError):
            self._writable = False
            logger.warning("settings_read_failed")

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        self._data[key] = value
        if self._writable:
            write_json(self.path, self._data, logger)
