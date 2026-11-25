"""Local preset definitions and the portable, capability-free custom preset format."""
from dataclasses import asdict, dataclass, replace
import json
import re
from uuid import uuid4


class InvalidPreset(ValueError):
    pass


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    instruction: str = ""
    model: str = ""
    timeout: int = 0  # inherit global timeout
    language: str = ""
    revision: int = 1

    @property
    def custom(self):
        return self.id.startswith("custom-")

    @property
    def label(self):
        if self.custom:
            return "Custom · " + self.name
        return f"{self.name} → {self.language}" if self.language else self.name

    @property
    def correction(self):
        return self.id in ("strict", "natural", "professional")


BUILTINS = {p.id: p for p in (
    Preset("raw", "Raw"),
    Preset("strict", "Smart Strict"),
    Preset("natural", "Smart Natural", "Smooth spoken phrasing. Preserve language, facts, names, numbers, negations and intent."),
    Preset("professional", "Smart Professional", "Rewrite in a professional register in the same language. Preserve facts, names, numbers, negations and intent."),
    Preset("prompt", "Prompt Engineer", "Rewrite the input as a clear, self-contained prompt. Preserve its constraints. Never answer the prompt."),
    Preset("translator", "Translator", "Translate the entire input faithfully, including questions and commands.", language="English"),
    Preset("formatter", "Formatter", "Organize the input with paragraphs or lists as appropriate. Preserve content, facts and numbers."),
    Preset("tasks", "Task Extractor", "List only tasks stated in the input, one task per line starting with '- '. Preserve owners and deadlines. If none exist, return exactly 'No tasks.'."),
    Preset("summary", "Summarizer", "Summarize the input concisely in its language. Preserve key facts and qualifications."),
)}

MAX_FILE = 64 * 1024


def valid_id(value):
    return isinstance(value, str) and (value in BUILTINS or bool(re.fullmatch(r"custom-[0-9a-f]{32}", value)))


def checked_string(value, limit, *, optional=False, multiline=False):
    if not isinstance(value, str) or len(value) > limit or (not optional and not value.strip()):
        raise InvalidPreset("invalid_text")
    if any(not c.isprintable() and not (multiline and c in "\r\n\t") for c in value):
        raise InvalidPreset("invalid_character")
    return value.strip()


def validate(preset):
    if not valid_id(preset.id) or type(preset.revision) is not int or preset.revision < 1:
        raise InvalidPreset("invalid_definition")
    name = checked_string(preset.name, 80)
    instruction = checked_string(preset.instruction, 4000, optional=not preset.custom, multiline=True)
    model = checked_string(preset.model, 200, optional=True)
    language = checked_string(preset.language, 64, optional=preset.id != "translator")
    if type(preset.timeout) is not int or not 0 <= preset.timeout <= 300:
        raise InvalidPreset("invalid_timeout")
    if not preset.custom:
        base = BUILTINS[preset.id]
        if name != base.name or instruction != base.instruction:
            raise InvalidPreset("builtin_definition")
    if language and preset.id != "translator":
        raise InvalidPreset("unexpected_language")
    return replace(preset, name=name, instruction=instruction, model=model, language=language)


def new_custom():
    return Preset("custom-" + uuid4().hex, "Custom preset")


def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidPreset("duplicate_field")
        result[key] = value
    return result


def import_custom(data):
    if len(data) > MAX_FILE:
        raise InvalidPreset("file_too_large")
    try:
        packet = json.loads(data.decode("utf-8-sig"), object_pairs_hook=unique_fields)
    except (UnicodeError, ValueError, RecursionError):
        raise InvalidPreset("invalid_json") from None
    if (not isinstance(packet, dict) or set(packet) != {"version", "preset"}
            or type(packet["version"]) is not int or packet["version"] != 1):
        raise InvalidPreset("unsupported_format")
    fields = packet["preset"]
    if not isinstance(fields, dict) or set(fields) != {"name", "instruction", "model", "timeout"}:
        raise InvalidPreset("unexpected_fields")
    return validate(replace(new_custom(), **fields))


def export_custom(preset):
    preset = validate(preset)
    if not preset.custom:
        raise InvalidPreset("custom_only")
    fields = asdict(preset)
    packet = {"version": 1, "preset": {k: fields[k] for k in ("name", "instruction", "model", "timeout")}}
    return (json.dumps(packet, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
