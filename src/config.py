"""Application configuration loaded from config.yaml."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


@dataclass
class ModelConfig:
    name: str = "large-v3-turbo"
    device: str = "cuda"
    compute_type: str = "int8_float16"


@dataclass
class AudioConfig:
    device: int | str | None = None
    sample_rate: int = 16000


@dataclass
class PostprocessConfig:
    enabled: bool = False
    base_url: str = "http://localhost:1234/v1"
    model: str = ""
    timeout_seconds: float = 10.0


@dataclass
class AppConfig:
    hotkey: str = "ctrl+windows"
    language: str = "auto"
    min_record_seconds: float = 0.3
    sounds: bool = True
    model: ModelConfig = field(default_factory=ModelConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    postprocess: PostprocessConfig = field(default_factory=PostprocessConfig)


def load_config(path: Path = CONFIG_PATH) -> AppConfig:
    """Load config.yaml, falling back to defaults for missing keys."""
    config = AppConfig()
    if not path.exists():
        logger.warning("Config file %s not found, using defaults", path)
        return config

    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    config.hotkey = raw.get("hotkey", config.hotkey)
    config.language = raw.get("language", config.language)
    config.min_record_seconds = float(raw.get("min_record_seconds", config.min_record_seconds))
    config.sounds = bool(raw.get("sounds", config.sounds))

    model = raw.get("model") or {}
    config.model = ModelConfig(
        name=model.get("name", config.model.name),
        device=model.get("device", config.model.device),
        compute_type=model.get("compute_type", config.model.compute_type),
    )

    audio = raw.get("audio") or {}
    config.audio = AudioConfig(
        device=audio.get("device", config.audio.device),
        sample_rate=int(audio.get("sample_rate", config.audio.sample_rate)),
    )

    pp = raw.get("postprocess") or {}
    config.postprocess = PostprocessConfig(
        enabled=bool(pp.get("enabled", config.postprocess.enabled)),
        base_url=pp.get("base_url", config.postprocess.base_url),
        model=pp.get("model", config.postprocess.model),
        timeout_seconds=float(pp.get("timeout_seconds", config.postprocess.timeout_seconds)),
    )

    return config
