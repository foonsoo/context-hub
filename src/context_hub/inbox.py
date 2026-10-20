"""Append-only evidence. Every knowledge page points back to a line here."""

from __future__ import annotations

import json
import os
import secrets as _random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .hub import Hub, now
from .secrets import redact

MAX_TEXT = 8000


def new_id(prefix: str) -> str:
    return f"{prefix}-{_random.token_hex(3)}"


@dataclass
class Entry:
    data: dict[str, Any]
    ref: str  # "inbox/2026/10/20/notes.jsonl#L3"

    def __getitem__(self, key: str) -> Any:
        return self.data.get(key)


def day_dir(hub: Hub) -> Path:
    stamp = now()
    return hub.inbox / f"{stamp:%Y}" / f"{stamp:%m}" / f"{stamp:%d}"


def append(hub: Hub, file: Path, records: list[dict[str, Any]]) -> list[str]:
    """Append records under a lock and return their evidence refs."""
    file.parent.mkdir(parents=True, exist_ok=True)
    refs: list[str] = []
    with hub.lock("inbox"):
        try:
            with open(file, "rb") as handle:
                count = sum(1 for _ in handle)
        except FileNotFoundError:
            count = 0
        payload = []
        for record in records:
            record = dict(record)
            record.setdefault("ts", now().isoformat())
            if isinstance(record.get("text"), str):
                text = redact(record["text"])
                if len(text) > MAX_TEXT:
                    text = text[:MAX_TEXT] + " …[truncated]"
                record["text"] = text
            payload.append(json.dumps(record, ensure_ascii=False) + "\n")
            count += 1
            refs.append(f"{file.relative_to(hub.root).as_posix()}#L{count}")
        fd = os.open(file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, "".join(payload).encode())
        finally:
            os.close(fd)
    return refs


def add_note(hub: Hub, record: dict[str, Any]) -> str:
    return append(hub, day_dir(hub) / "notes.jsonl", [record])[0]


def read_lines(hub: Hub, file: Path, start: int = 0) -> Iterator[Entry]:
    """Yield entries after line ``start`` (0-based count of lines already seen)."""
    rel = file.relative_to(hub.root).as_posix()
    with open(file, encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle, start=1):
            if number <= start or not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield Entry(data=data, ref=f"{rel}#L{number}")


def resolve_ref(hub: Hub, ref: str) -> dict[str, Any] | None:
    path, _, line = ref.partition("#L")
    try:
        target = int(line.split("-")[0])
        with open(hub.root / path, encoding="utf-8") as handle:
            for number, raw in enumerate(handle, start=1):
                if number == target:
                    return json.loads(raw)
    except (OSError, ValueError):
        return None
    return None


def files(hub: Hub) -> list[Path]:
    if not hub.inbox.exists():
        return []
    return sorted(p for p in hub.inbox.rglob("*.jsonl") if p.is_file())
