"""Claude Code transcripts: ``~/.claude/projects/<dir>/<session>.jsonl``."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from . import Adapter, Event, home_path
from ._common import clean_user_text, read_json_lines, short

_TOOL_FIELDS = ("command", "file_path", "path", "pattern", "url", "query", "description")


def discover() -> list[Path]:
    root = home_path(".claude", env="CLAUDE_CONFIG_DIR") / "projects"
    if not root.is_dir():
        return []
    return sorted(root.glob("*/*.jsonl"))


def _tool_summary(block: dict) -> str:
    name = block.get("name") or "tool"
    data = block.get("input") or {}
    detail = next((data[k] for k in _TOOL_FIELDS if isinstance(data, dict) and data.get(k)), "")
    return f"tool {name}: {short(detail, 140)}" if detail else f"tool {name}"


def parse(path: Path, offset: int) -> Iterator[tuple[int, Event | None]]:
    for position, data in read_json_lines(path, offset):
        if not data or data.get("isSidechain") or data.get("isMeta"):
            yield position, None
            continue
        kind = data.get("type")
        message = data.get("message") if isinstance(data.get("message"), dict) else {}
        session = str(data.get("sessionId") or path.stem)
        common = dict(ts=str(data.get("timestamp") or ""), session=session, cwd=data.get("cwd"))
        content = message.get("content")
        if kind == "user":
            if isinstance(content, str):
                text = clean_user_text(content)
            elif isinstance(content, list):
                text = clean_user_text("\n".join(
                    b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
                ))
            else:
                text = ""
            yield position, Event(role="user", text=text, **common) if text else None
        elif kind == "assistant" and isinstance(content, list):
            parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text", "").strip():
                    parts.append(("assistant", block["text"].strip()))
                elif block.get("type") == "tool_use":
                    parts.append(("tool", _tool_summary(block)))
            if not parts:
                yield position, None
                continue
            for role, text in parts:  # one line may hold text and tool calls
                yield position, Event(role=role, text=text, **common)
        else:
            yield position, None


ADAPTER = Adapter(name="claude-code", discover=discover, parse=parse)
