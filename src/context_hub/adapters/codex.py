"""Codex CLI rollouts: ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from . import Adapter, Event, home_path
from ._common import clean_user_text, read_json_lines, short


def discover() -> list[Path]:
    root = home_path(".codex", env="CODEX_HOME") / "sessions"
    if not root.is_dir():
        return []
    return sorted(root.rglob("rollout-*.jsonl"))


def _meta(path: Path) -> tuple[str, str | None]:
    session, cwd = path.stem, None
    for _, data in read_json_lines(path, 0):
        if not data:
            continue
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else data
        if data.get("type") in ("session_meta", "turn_context") or "instructions" in data:
            session = str(payload.get("id") or session)
            cwd = payload.get("cwd") or cwd
            if cwd:
                break
        if data.get("type") == "response_item":
            break
    return session, cwd


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") in ("input_text", "output_text", "text")
        )
    return ""


def parse(path: Path, offset: int) -> Iterator[tuple[int, Event | None]]:
    session, cwd = _meta(path)
    for position, data in read_json_lines(path, offset):
        if not data:
            yield position, None
            continue
        kind = data.get("type")
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else data
        if kind == "turn_context" and payload.get("cwd"):
            cwd = payload["cwd"]
        item_type = payload.get("type") if kind == "response_item" else kind
        ts = str(data.get("timestamp") or "")
        if item_type == "message":
            role = payload.get("role")
            text = _text(payload.get("content"))
            if role == "user":
                text = clean_user_text(text)
            elif role != "assistant":
                text = ""
            yield position, Event(ts=ts, session=session, cwd=cwd, role=role, text=text.strip()) if text.strip() else None
        elif item_type in ("function_call", "local_shell_call", "custom_tool_call"):
            name = payload.get("name") or item_type
            args = payload.get("arguments") or payload.get("action") or payload.get("input") or ""
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    pass
            if isinstance(args, dict):
                args = args.get("command") or args.get("cmd") or args.get("path") or args
            yield position, Event(ts=ts, session=session, cwd=cwd, role="tool", text=f"tool {name}: {short(args, 140)}")
        else:
            yield position, None


ADAPTER = Adapter(name="codex", discover=discover, parse=parse)
