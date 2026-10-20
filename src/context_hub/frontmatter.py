"""A small YAML subset for Markdown frontmatter.

Supported shapes are exactly what knowledge pages need, so hand edits stay
predictable: ``key: scalar``, ``key: [a, b]``, block lists (``- item``) and
one level of nested mappings (``links:`` followed by ``  about: [x]``).
"""

from __future__ import annotations

import json
import re
from typing import Any

_PLAIN_UNSAFE = re.compile(
    r"""^[\s\-\[\]{}"'#&*!|>%@`,?]|[:#]\s|:$|\s$|^(true|false|null|~|yes|no|-?\d[\d._:-]*)$""",
    re.IGNORECASE,
)


class FrontmatterError(ValueError):
    pass


def _dump_scalar(value: Any, in_list: bool = False) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if (
        text == ""
        or "\n" in text
        or _PLAIN_UNSAFE.search(text)
        or (in_list and any(ch in text for ch in ",[]"))
    ):
        return json.dumps(text, ensure_ascii=False)
    return text


def _dump_list(values: list) -> str:
    return "[" + ", ".join(_dump_scalar(v, in_list=True) for v in values) + "]"


def dump(meta: dict, body: str) -> str:
    lines = ["---"]
    for key, value in meta.items():
        if isinstance(value, dict):
            if not value:
                lines.append(f"{key}: {{}}")
                continue
            lines.append(f"{key}:")
            for sub, sub_value in value.items():
                rendered = (
                    _dump_list(sub_value)
                    if isinstance(sub_value, list)
                    else _dump_scalar(sub_value)
                )
                lines.append(f"  {sub}: {rendered}")
        elif isinstance(value, list):
            lines.append(f"{key}: {_dump_list(value)}")
        else:
            lines.append(f"{key}: {_dump_scalar(value)}")
    lines.append("---")
    text = "\n".join(lines) + "\n"
    body = body.strip("\n")
    if body:
        text += "\n" + body + "\n"
    return text


def _split_flow(inner: str) -> list[str]:
    items, current, quote, escape = [], [], None, False
    for ch in inner:
        if quote:
            current.append(ch)
            if escape:
                escape = False
            elif ch == "\\" and quote == '"':
                escape = True
            elif ch == quote:
                quote = None
        elif ch in "\"'" and not "".join(current).strip():
            quote = ch
            current.append(ch)
        elif ch == ",":
            items.append("".join(current))
            current = []
        else:
            current.append(ch)
    if quote:
        raise FrontmatterError(f"unterminated quote in list: [{inner}]")
    tail = "".join(current)
    if tail.strip() or items:
        items.append(tail)
    return [item.strip() for item in items if item.strip()]


def _parse_scalar(raw: str) -> Any:
    raw = raw.strip()
    if raw == "" or raw in ("null", "~"):
        return None
    if raw.startswith('"'):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FrontmatterError(f"bad quoted string: {raw}") from exc
    if raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
        return raw[1:-1].replace("''", "'")
    if raw.startswith("["):
        if not raw.endswith("]"):
            raise FrontmatterError(f"unterminated list: {raw}")
        return [_parse_scalar(item) for item in _split_flow(raw[1:-1])]
    if raw == "{}":
        return {}
    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    return raw


def parse(text: str) -> tuple[dict, str]:
    """Return ``(meta, body)``. Text without frontmatter yields ``({}, text)``."""
    text = text.lstrip("﻿")
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return {}, text
    end = next(
        (i for i in range(1, len(lines)) if lines[i].strip() == "---"), None
    )
    if end is None:
        raise FrontmatterError("frontmatter is not closed with ---")
    meta: dict[str, Any] = {}
    current_key: str | None = None
    for number, line in enumerate(lines[1:end], start=2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indented = line[0] in " \t"
        stripped = line.strip()
        if indented:
            if current_key is None:
                raise FrontmatterError(f"line {number}: unexpected indentation")
            container = meta[current_key]
            if stripped.startswith("- ") or stripped == "-":
                if container is None or container == {}:
                    container = meta[current_key] = []
                if not isinstance(container, list):
                    raise FrontmatterError(f"line {number}: mixed list and map")
                container.append(_parse_scalar(stripped[1:]))
                continue
            if ":" not in stripped:
                raise FrontmatterError(f"line {number}: expected 'key: value'")
            sub, _, raw = stripped.partition(":")
            if container is None or container == []:
                container = meta[current_key] = {}
            if not isinstance(container, dict):
                raise FrontmatterError(f"line {number}: mixed list and map")
            container[sub.strip()] = _parse_scalar(raw)
            continue
        if ":" not in stripped:
            raise FrontmatterError(f"line {number}: expected 'key: value'")
        key, _, raw = stripped.partition(":")
        key = key.strip()
        value = _parse_scalar(raw)
        meta[key] = value
        current_key = key if raw.strip() == "" else None
    body = "\n".join(lines[end + 1 :]).strip("\n")
    return meta, body
