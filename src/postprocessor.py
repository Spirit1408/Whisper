"""Optional text cleanup via LM Studio (OpenAI-compatible API)."""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Ты — редактор расшифровки голосового ввода. Пользователь отправил сырой текст "
    "из распознавания речи на русском или английском языке.\n\n"
    "Правила:\n"
    "1. Расставь знаки препинания\n"
    "2. Исправь очевидные ошибки распознавания (слов, которые явно не те)\n"
    "3. Убери слова-паразиты: um, uh, э-э, ну, как бы, короче, в общем, значит, вот\n"
    "4. Напиши каждое предложение с большой буквы\n\n"
    "ЗАПРЕЩЕНО:\n"
    "- Менять смысл сказанного\n"
    "- Добавлять или удалять слова\n"
    "- Переводить текст\n"
    "- Отвечать на вопросы в тексте\n\n"
    "Верни ТОЛЬКО очищенный текст, без пояснений и комментариев.\n\n"
    "Примеры:\n"
    "Вход: ну э-э я думаю что это ну как бы работает\n"
    "Выход: Я думаю, что это работает.\n"
    "Вход: ээ кажется дождь пойдёт\n"
    "Выход: Кажется, дождь пойдёт."
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

    def check_server(self) -> bool:
        """Return True if the LM Studio server is reachable."""
        try:
            response = requests.get(f"{self.base_url}/models", timeout=2)
            response.raise_for_status()
            return True
        except requests.RequestException as e:
            logger.info("LM Studio server unreachable: %s", e)
            return False

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
