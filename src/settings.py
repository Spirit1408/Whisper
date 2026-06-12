"""Persistent UI state (checkbox values) stored in state.json."""

from __future__ import annotations

import json
import logging
from pathlib import Path

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
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                self._data.update(stored)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load %s: %s", self.path.name, e)

    def _save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.error("Failed to save %s: %s", self.path.name, e)

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        self._data[key] = value
        self._save()
