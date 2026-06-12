"""Optional text cleanup via LM Studio (OpenAI-compatible API)."""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a dictation post-processor. The user message is raw speech-to-text output "
    "in Russian or English. Fix punctuation and capitalization, remove filler words "
    "(um, uh, э-э, ну, как бы, короче), and fix obvious recognition mistakes. "
    "Do NOT change the meaning, do NOT add or remove content, do NOT translate, "
    "do NOT answer questions in the text. Return ONLY the cleaned text."
)


class PostProcessor:
    """Sends text to LM Studio for cleanup. Falls back to raw text on any failure."""

    def __init__(
        self,
        base_url: str = "http://localhost:1234/v1",
        model: str = "",
        timeout_seconds: float = 10.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout_seconds
        self.enabled = False

    def process(self, text: str) -> str:
        """Return cleaned text, or the original text if disabled or LM Studio fails."""
        if not self.enabled or not text:
            return text
        try:
            payload = {
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
                "temperature": 0.1,
                "stream": False,
            }
            if self.model:
                payload["model"] = self.model
            response = requests.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
            cleaned = response.json()["choices"][0]["message"]["content"].strip()
            if not cleaned:
                logger.warning("LM Studio returned empty text, using raw transcription")
                return text
            logger.info("Post-processed: %s", cleaned)
            return cleaned
        except (requests.RequestException, KeyError, IndexError, ValueError) as e:
            logger.warning("Post-processing failed (%s), using raw transcription", e)
            return text
