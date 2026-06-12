"""Persistent transcription history stored in history.json."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

HISTORY_PATH = Path(__file__).resolve().parent.parent / "history.json"
MAX_ENTRIES = 50


class HistoryStore:
    """JSON-backed list of transcription entries (newest first)."""

    def __init__(self, path: Path = HISTORY_PATH):
        self.path = path
        self.entries: list[dict] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, list):
                self.entries = stored[:MAX_ENTRIES]
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load %s: %s", self.path.name, e)

    def _save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.entries, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.error("Failed to save %s: %s", self.path.name, e)

    def add(self, text: str) -> dict:
        """Prepend a new entry and return it."""
        entry = {"id": f"{time.time():.6f}", "text": text, "ts": time.time()}
        self.entries.insert(0, entry)
        del self.entries[MAX_ENTRIES:]
        self._save()
        return entry

    def remove(self, entry_id: str) -> None:
        self.entries = [e for e in self.entries if e.get("id") != entry_id]
        self._save()
