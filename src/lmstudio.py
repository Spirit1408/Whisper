"""Drive the LM Studio local server and models through the `lms` CLI.

Status is probed over the OpenAI-compatible HTTP endpoint; lifecycle actions
(start the server, list/load models) shell out to the `lms` CLI so the
app can bring LM Studio to a ready state on demand.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import requests

from local_http import LocalEndpointError, local_base, request

logger = logging.getLogger(__name__)

# Suppress console windows for child processes when running under pythonw.exe.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class LMStudioManager:
    """Locate and control LM Studio via its `lms` command-line tool."""

    def __init__(self, base_url: str = "http://localhost:1234/v1", closing=None):
        self.base_url = base_url.rstrip("/")
        self._closing = closing if closing is not None else threading.Event()
        self._lms_path: str | None = None
        self._lms_searched = False

    # --- CLI discovery -------------------------------------------------

    def lms_path(self) -> str | None:
        """Path to the `lms` executable, cached after the first lookup."""
        if not self._lms_searched:
            self._lms_path = self._find_lms()
            self._lms_searched = True
            logger.info("lms_cli_available=%s", bool(self._lms_path))
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
        try:
            return local_base(self.base_url) == "http://127.0.0.1:1234/v1" and self.lms_path() is not None
        except LocalEndpointError:
            return False

    # --- low-level CLI runner -----------------------------------------

    def _run(self, args: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
        if self._closing.is_set():
            return None
        # The CLI manages the default local service, not an arbitrary configured port.
        try:
            if local_base(self.base_url) != "http://127.0.0.1:1234/v1":
                return None
        except LocalEndpointError:
            return None
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
        except (subprocess.TimeoutExpired, OSError, UnicodeError):
            logger.warning("lms_command_failed")
            return None
        if result.returncode != 0:
            logger.warning("lms_exit code=%d", result.returncode)
        return result

    def _run_json(self, args: list[str], timeout: float):
        result = self._run(args, timeout)
        if result is None or result.returncode != 0:
            return None
        try:
            return json.loads(result.stdout)
        except ValueError:
            logger.warning("lms_invalid_json")
            return None

    # --- status --------------------------------------------------------

    def is_server_up(self) -> bool:
        """Quick HTTP probe of the OpenAI-compatible endpoint."""
        if self._closing.is_set():
            return False
        try:
            request("GET", self.base_url, "/v1/models", timeout=2)
            return True
        except (requests.RequestException, LocalEndpointError):
            return False

    def wait_for_server(self, attempts: int = 20, interval: float = 0.5) -> bool:
        """Poll until the server answers or the attempts run out."""
        for _ in range(attempts):
            if self.is_server_up():
                return True
            if self._closing.wait(interval):
                return False
        return self.is_server_up()

    def list_llms(self) -> list[tuple[str, str]]:
        """Downloaded LLMs as [(model_key, display_name)] (embeddings excluded)."""
        data = self._run_json(["ls", "--json"], timeout=15)
        if not isinstance(data, list):
            return []
        return [
            (m["modelKey"], m["displayName"] if isinstance(m.get("displayName"), str) else m["modelKey"])
            for m in data
            if isinstance(m, dict) and m.get("type") == "llm" and isinstance(m.get("modelKey"), str)
            and m["modelKey"]
        ]

    def loaded_llms(self) -> dict[str, str]:
        """Map downloaded keys to unambiguous inference instance identifiers."""
        data = self._run_json(["ps", "--json"], timeout=15)
        if not isinstance(data, list):
            # /v1/models can include *unloaded* JIT candidates. Require an
            # explicit loaded state when there is no usable CLI response.
            if self._closing.is_set():
                return {}
            try:
                packet = request("GET", self.base_url, "/api/v0/models", timeout=2).json()
                return {m["id"]: m["id"] for m in packet["data"] if isinstance(m, dict)
                        and m.get("type") == "llm" and m.get("state") == "loaded"
                        and isinstance(m.get("id"), str)}
            except (requests.RequestException, ValueError, KeyError, TypeError):
                return {}
        instances: dict[str, list[str]] = {}
        for m in data:
            if isinstance(m, dict) and m.get("type") == "llm":
                key = m.get("modelKey")
                identifier = m.get("identifier") or key
                if isinstance(key, str) and isinstance(identifier, str) and identifier:
                    instances.setdefault(key, []).append(identifier)
        return {key: ids[0] for key, ids in instances.items() if len(ids) == 1}

    # --- actions -------------------------------------------------------

    def start_server(self) -> bool:
        """Start only the default loopback listener after an explicit request."""
        result = self._run(["server", "start", "--bind", "127.0.0.1", "--port", "1234"], timeout=30)
        return result is not None and result.returncode == 0

    def load_model(self, model_key: str, timeout: float = 300.0) -> bool:
        """Load a model into memory. Blocks until loaded or the timeout."""
        if not model_key or model_key.startswith("-") or model_key not in dict(self.list_llms()):
            return False
        running = self._run_json(["ps", "--json"], timeout=15)
        if not isinstance(running, list):
            return False
        matches = [m for m in running if isinstance(m, dict) and m.get("modelKey") == model_key]
        if matches:
            return len(matches) == 1
        result = self._run(["load", model_key, "-y"], timeout=timeout)
        return result is not None and result.returncode == 0
