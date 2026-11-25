"""Preferences cached in RAM for the GUI and model selection."""
import logging
from history import StorageError

logger = logging.getLogger(__name__)


class SettingsStore:
    def __init__(self, store):
        self.store = store
        try:
            self._data = store.preferences()
        except StorageError:
            self._data = {}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value
        try:
            self.store.set_preference(key, value)
        except StorageError:
            logger.warning("preference_not_saved")
            return False
        return True
