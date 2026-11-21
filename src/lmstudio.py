"""Drive the LM Studio local server and models through the `lms` CLI.

Status is probed over the OpenAI-compatible HTTP endpoint; lifecycle actions
(start the server, list/load/unload models) shell out to the `lms` CLI so the
app can bring LM Studio to a ready state on demand.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

# Suppress console windows for child processes when running under pythonw.exe.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class LMStudioManager:
    """Locate and control LM Studio via its `lms` command-line tool."""

    def __init__(self, base_url: str = "http://localhost:1234/v1"):
        self.base_url = base_url.rstrip("/")
        self._lms_path: str | None = None
        self._lms_searched = False

    # --- CLI discovery -------------------------------------------------

    def lms_path(self) -> str | None:
        """Path to the `lms` executable, cached after the first lookup."""
        if not self._lms_searched:
            self._lms_path = self._find_lms()
            self._lms_searched = True
            logger.info("lms CLI: %s", self._lms_path or "not found")
        return self._lms_path

    @staticmethod
    def _find_lms() -> str | None:
        on_path = shutil.which("lms")
        if on_path:
            return on_path
        home = Path.home()
        candidates = [
            home / ".lmstudio" / "bin" / "lms.exe",
            home / ".lmstudio" / "bin" / "lms",
            home / ".cache" / "lm-studio" / "bin" / "lms.exe",
        ]
        for path in candidates:
            if path.exists():
                return str(path)
        return None

    @property
    def available(self) -> bool:
        """True if the `lms` CLI is present (the server can be controlled)."""
        return self.lms_path() is not None

    # --- low-level CLI runner -----------------------------------------

    def _run(self, args: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
        lms = self.lms_path()
        if lms is None:
            return None
        try:
            result = subprocess.run(
                [lms, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                creationflags=_NO_WINDOW,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning("`lms %s` failed: %s", " ".join(args), e)
            return None
        if result.returncode != 0:
            logger.warning(
                "`lms %s` exited %s: %s",
                " ".join(args), result.returncode, (result.stderr or "").strip(),
            )
        return result

    def _run_json(self, args: list[str], timeout: float):
        result = self._run(args, timeout)
        if result is None or result.returncode != 0:
            return None
        try:
            return json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("Could not parse `lms %s` JSON: %s", " ".join(args), e)
            return None

    # --- status --------------------------------------------------------

    def is_server_up(self) -> bool:
        """Quick HTTP probe of the OpenAI-compatible endpoint."""
        try:
            response = requests.get(f"{self.base_url}/models", timeout=2)
            response.raise_for_status()
            return True
        except requests.RequestException:
            return False

    def wait_for_server(self, attempts: int = 20, interval: float = 0.5) -> bool:
        """Poll until the server answers or the attempts run out."""
        for _ in range(attempts):
            if self.is_server_up():
                return True
            time.sleep(interval)
        return self.is_server_up()

    def list_llms(self) -> list[tuple[str, str]]:
        """Downloaded LLMs as [(model_key, display_name)] (embeddings excluded)."""
        data = self._run_json(["ls", "--json"], timeout=15)
        if not isinstance(data, list):
            return []
        return [
            (m["modelKey"], m.get("displayName") or m["modelKey"])
            for m in data
            if isinstance(m, dict) and m.get("type") == "llm" and m.get("modelKey")
        ]

    def loaded_llms(self) -> list[str]:
        """Model keys of currently loaded LLMs (embeddings excluded)."""
        data = self._run_json(["ps", "--json"], timeout=15)
        if not isinstance(data, list):
            return []
        return [
            m["modelKey"]
            for m in data
            if isinstance(m, dict) and m.get("type") == "llm" and m.get("modelKey")
        ]

    # --- actions -------------------------------------------------------

    def start_server(self) -> bool:
        """Start the local server (restores the last server state)."""
        result = self._run(["server", "start"], timeout=30)
        return result is not None and result.returncode == 0

    def load_model(self, model_key: str, timeout: float = 300.0) -> bool:
        """Load a model into memory. Blocks until loaded or the timeout."""
        result = self._run(["load", model_key, "-y"], timeout=timeout)
        return result is not None and result.returncode == 0

    def unload_other_llms(self, keep: str) -> None:
        """Unload every loaded LLM except ``keep`` to bound VRAM use."""
        for key in self.loaded_llms():
            if key != keep:
                self._run(["unload", key], timeout=30)
