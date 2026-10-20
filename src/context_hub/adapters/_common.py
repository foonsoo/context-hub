from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterator

_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_INJECTED_PREFIXES = (
    "<command-", "<local-command", "caveat:", "<environment_context>", "<user_instructions>",
    "<permissions instructions>", "# agents.md instructions", "<turn_aborted>", "[request interrupted",
)


def clean_user_text(text: str) -> str:
    text = _REMINDER.sub("", text or "").strip()
    if text.casefold().startswith(_INJECTED_PREFIXES):
        return ""
    return text


def read_json_lines(path: Path, offset: int) -> Iterator[tuple[int, dict | None]]:
    """Yield (offset after line, parsed object) for complete lines after ``offset``."""
    with open(path, "rb") as handle:
        handle.seek(offset)
        position = offset
        for raw in handle:
            if not raw.endswith(b"\n"):
                return  # partial line still being written
            position += len(raw)
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                data = None
            yield position, data if isinstance(data, dict) else None


def short(value: object, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
