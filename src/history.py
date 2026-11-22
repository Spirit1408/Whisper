"""Persistent transcription history stored in history.json."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from json_store import write_json

logger = logging.getLogger(__name__)

HISTORY_PATH = Path(__file__).resolve().parent.parent / "history.json"
MAX_ENTRIES = 50


class HistoryStore:
    """JSON-backed list of transcription entries (newest first)."""

    def __init__(self, path: Path = HISTORY_PATH):
        self.path = path
        self.entries: list[dict] = []
        self.writable = True
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                stored = json.load(f)
            if not isinstance(stored, list):
                raise ValueError
            self.entries = stored[:MAX_ENTRIES]
        except (ValueError, OSError):
            self.writable = False
            logger.warning("history_read_failed")

    def _save(self, entries: list[dict]) -> bool:
        if not self.writable or not write_json(self.path, entries, logger):
            return False
        self.entries = entries
        return True

    def add(self, text: str) -> dict | None:
        """Prepend a new entry and return it."""
        entry = {"id": f"{time.time():.6f}", "text": text, "ts": time.time()}
        return entry if self._save([entry, *self.entries[:MAX_ENTRIES - 1]]) else None

    def remove(self, entry_id: str) -> bool:
        return self._save([e for e in self.entries if e.get("id") != entry_id])
