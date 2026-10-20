"""Collect client transcripts after the fact so context survives a forgotten `note`.

Each adapter turns a client's local log into common events
``{ts, client, session, cwd, role, text}`` and appends them to the inbox. Read
offsets live in ``.state/adapters.json`` so every run only reads new lines.
Transcript formats are not public APIs: unknown lines are skipped, never fatal.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator

from .. import inbox
from ..hub import Hub
from ..identity import register_cwd, repo_info, resolve

MAX_EVENT_TEXT = 4000


@dataclass
class Event:
    ts: str
    session: str
    cwd: str | None
    role: str  # user | assistant | tool
    text: str


@dataclass
class Adapter:
    name: str
    discover: Callable[[], Iterable[Path]]
    parse: Callable[[Path, int], Iterator[tuple[int, Event | None]]]


def _all() -> list[Adapter]:
    from . import claude_code, codex

    return [claude_code.ADAPTER, codex.ADAPTER]


def adapters(names: Iterable[str] | None = None) -> list[Adapter]:
    found = _all()
    if names:
        wanted = set(names)
        found = [a for a in found if a.name in wanted]
    return found


def ingest_file(hub: Hub, adapter: Adapter, path: Path, state: dict) -> int:
    """Append new events from one transcript file. Returns events written."""
    key = f"{adapter.name}:{path}"
    info = state.get(key) or {}
    try:
        stat = path.stat()
    except OSError:
        return 0
    offset = int(info.get("offset", 0))
    if stat.st_size < offset:  # file was rewritten; start over
        offset = 0
    if stat.st_size == offset:
        return 0
    events: list[Event] = []
    new_offset = offset
    for end_offset, event in adapter.parse(path, offset):
        new_offset = end_offset
        if event and event.text.strip():
            events.append(event)
    written = 0
    by_session: dict[str, list[Event]] = {}
    for event in events:
        by_session.setdefault(event.session, []).append(event)
    for session, items in by_session.items():
        cwd = next((e.cwd for e in items if e.cwd), info.get("cwd"))
        project = _project_for(hub, cwd, info)
        if project is False:
            continue
        info["cwd"] = cwd
        target = inbox.day_dir(hub) / f"{adapter.name}-{_safe(session)}.jsonl"
        records = [
            {"ts": e.ts, "client": adapter.name, "session": session, "cwd": e.cwd or cwd,
             "project": project, "role": e.role, "text": e.text[:MAX_EVENT_TEXT],
             "source": "transcript"}
            for e in items
        ]
        inbox.append(hub, target, records)
        written += len(records)
    info["offset"] = new_offset
    state[key] = info
    return written


def _project_for(hub: Hub, cwd: str | None, info: dict) -> str | None | bool:
    """Project slug, None for unknown, or False when capture is disabled."""
    if not cwd:
        return None
    res = resolve(hub, cwd=cwd, for_write=True)
    slug = res.slug
    if not slug and repo_info(cwd):
        # Auto-register git repositories only; scratch folders stay unassigned.
        slug = register_cwd(hub, cwd)
    if slug:
        project = hub.projects().get(slug)
        if project and not project.capture:
            return False
    return slug


def _safe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in name)[:80] or "session"


def ingest(hub: Hub, names: Iterable[str] | None = None, paths: Iterable[Path] | None = None,
           quiet_seconds: float = 0) -> dict[str, int]:
    """Ingest transcripts. ``paths`` limits to specific files (e.g. from a hook)."""
    import time

    results: dict[str, int] = {}
    with hub.lock("adapters"):
        state = hub.read_state("adapters")
        for adapter in adapters(names):
            files = list(paths) if paths is not None else list(adapter.discover())
            count = 0
            for path in files:
                path = Path(path)
                if quiet_seconds:
                    try:
                        if time.time() - path.stat().st_mtime < quiet_seconds:
                            continue
                    except OSError:
                        continue
                try:
                    count += ingest_file(hub, adapter, path, state)
                except Exception as exc:  # one bad file must not stop the rest
                    hub.log(f"ingest {adapter.name} {path}: {exc}")
            results[adapter.name] = count
        hub.write_state("adapters", state)
    return results


def home_path(*parts: str, env: str | None = None) -> Path:
    if env and os.environ.get(env):
        return Path(os.environ[env]).expanduser()
    return Path.home().joinpath(*parts)
