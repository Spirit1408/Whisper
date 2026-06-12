"""Windows autostart management via the HKCU Run registry key."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "WhisperDictation"

_LAUNCHER = Path(__file__).resolve().parent.parent / "run_hidden.vbs"


def _command() -> str:
    return f'wscript.exe "{_LAUNCHER}"'


def is_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
        return True
    except OSError:
        return False


def set_enabled(enabled: bool) -> bool:
    """Add/remove the Run entry. Returns True on success."""
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _command())
                logger.info("Autostart enabled: %s", _command())
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
                logger.info("Autostart disabled")
        return True
    except OSError as e:
        logger.error("Failed to update autostart registry entry: %s", e)
        return False
