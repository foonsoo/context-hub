"""Knowledge pages (graph nodes) and per-project NOW.md files."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from . import frontmatter
from .hub import Hub, now, slugify, today
from .inbox import new_id

TYPES = {
    "decision": "decisions",
    "fact": "facts",
    "constraint": "constraints",
    "concept": "concepts",
    "procedure": "procedures",
    "preference": "preferences",
    "task": "tasks",
}
TYPE_ALIASES = {
    "summary": "fact", "other": "fact", "note": "fact", "todo": "task",
    "rule": "constraint", "howto": "procedure",
}
STATUSES = ("proposed", "active", "superseded", "disputed", "archived")
EDGE_TYPES = ("supersedes", "depends_on", "about", "contradicts", "related")
NOW_SECTIONS = ("목표", "진행 중", "다음", "막힘", "최근 완료", "최근 결정")
NOW_LIMIT = 1500


def normalize_type(kind: str | None) -> str | None:
    if not kind:
        return None
    kind = kind.strip().casefold()
    kind = TYPE_ALIASES.get(kind, kind)
    return kind if kind in TYPES else None


def guess_type(text: str) -> str:
    head = text.strip().casefold()[:40]
    rules = [
        ("decision", ("결정", "decided", "decision", "합의", "하기로")),
        ("task", ("todo", "할 일", "할일", "해야 ", "task:")),
        ("preference", ("선호", "prefer", "좋아함", "원함")),
        ("constraint", ("제약", "금지", "must not", "never ", "반드시")),
        ("procedure", ("절차", "방법:", "how to", "실행 방법", "순서")),
    ]
    for kind, words in rules:
        if any(word in head for word in words):
            return kind
    return "fact"


def body_hash(body: str) -> str:
    return hashlib.sha1(body.strip().encode()).hexdigest()[:12]


@dataclass
class Page:
    meta: dict[str, Any]
    body: str
    path: Path | None = None

    @property
    def id(self) -> str:
        return str(self.meta.get("id"))

    @property
    def type(self) -> str:
        return str(self.meta.get("type") or "fact")

    @property
    def status(self) -> str:
        return str(self.meta.get("status") or "proposed")

    @property
    def project(self) -> str:
        return str(self.meta.get("project") or "global")

    @property
    def title(self) -> str:
        return str(self.meta.get("title") or self.id)

    @property
    def sources(self) -> list[str]:
        return list(self.meta.get("sources") or [])

    @property
    def links(self) -> dict[str, list[str]]:
        raw = self.meta.get("links") or {}
        return {k: list(v or []) for k, v in raw.items()} if isinstance(raw, dict) else {}

    @property
    def done(self) -> bool:
        return bool(self.meta.get("done"))

    def human_edited(self) -> bool:
        """True when the body changed since the curator last wrote it."""
        stored = self.meta.get("curator_hash")
        return bool(stored) and stored != body_hash(self.body)

    def wikilinks(self) -> list[str]:
        return [m.split("|", 1)[0].strip() for m in re.findall(r"\[\[([^\]]+)\]\]", self.body)]


def new_page(kind: str, title: str, body: str, project: str | None, sources: list[str],
             status: str = "proposed", links: dict[str, list[str]] | None = None) -> Page:
    kind = normalize_type(kind) or "fact"
    stamp = today()
    meta: dict[str, Any] = {
        "id": new_id(kind[:3]),
        "type": kind,
        "status": status,
        "project": project or "global",
        "title": title.strip()[:120] or "(untitled)",
        "sources": sources,
        "links": links or {},
        "created": stamp,
        "updated": stamp,
    }
    return Page(meta=meta, body=body.strip())


def title_from(text: str, limit: int = 60) -> str:
    first = next((line.strip(" #-*>\t") for line in text.strip().splitlines() if line.strip()), "")
    first = re.sub(r"^(결정|decision|todo|할 ?일|사실|fact|선호|제약|절차)\s*[:：]\s*", "", first, flags=re.I)
    return first if len(first) <= limit else first[: limit - 1].rstrip() + "…"


def page_path(hub: Hub, page: Page) -> Path:
    folder = hub.project_dir(page.project) / TYPES.get(page.type, "facts")
    return folder / f"{slugify(page.title, 40)}-{page.id}.md"


def save(hub: Hub, page: Page, by_curator: bool = True) -> Page:
    page.meta["updated"] = today()
    if by_curator:
        page.meta["curator_hash"] = body_hash(page.body)
    target = page.path or page_path(hub, page)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(frontmatter.dump(page.meta, page.body), encoding="utf-8")
    os.replace(tmp, target)
    page.path = target
    return page


def load(path: Path) -> Page:
    meta, body = frontmatter.parse(path.read_text(encoding="utf-8"))
    return Page(meta=meta, body=body, path=path)


def iter_pages(hub: Hub) -> Iterator[Page]:
    if not hub.knowledge.exists():
        return
    for path in sorted(hub.knowledge.rglob("*.md")):
        if path.name == "NOW.md":
            continue
        try:
            page = load(path)
        except (OSError, frontmatter.FrontmatterError, UnicodeDecodeError) as exc:
            hub.log(f"skip unreadable page {path}: {exc}")
            continue
        if page.meta.get("id"):
            yield page


def find(hub: Hub, page_id: str) -> Page | None:
    for page in iter_pages(hub):
        if page.id == page_id:
            return page
    return None


# NOW.md ------------------------------------------------------------------

@dataclass
class Now:
    project: str
    title: str
    sections: dict[str, list[str]] = field(default_factory=dict)
    updated: str = ""

    def render(self) -> str:
        meta = {"project": self.project, "updated": self.updated or now().isoformat()}
        lines = [f"# NOW · {self.title}", ""]
        for name in NOW_SECTIONS:
            items = self.sections.get(name) or []
            if not items:
                continue
            lines.append(f"## {name}")
            lines.extend(f"- {item}" for item in items)
            lines.append("")
        return frontmatter.dump(meta, "\n".join(lines))

    def text(self) -> str:
        return frontmatter.parse(self.render())[1]

    def is_empty(self) -> bool:
        return not any(self.sections.values())


def load_now(hub: Hub, slug: str, title: str | None = None) -> Now:
    path = hub.now_path(slug)
    result = Now(project=slug, title=title or slug)
    if not path.exists():
        return result
    meta, body = frontmatter.parse(path.read_text(encoding="utf-8"))
    result.updated = str(meta.get("updated") or "")
    current = None
    for line in body.splitlines():
        if line.startswith("# NOW"):
            result.title = title or line.split("·", 1)[-1].strip() or result.title
        elif line.startswith("## "):
            current = line[3:].strip()
            result.sections.setdefault(current, [])
        elif current and line.strip().startswith("- "):
            result.sections[current].append(line.strip()[2:].strip())
    return result


def _similar(a: str, b: str) -> bool:
    na, nb = re.sub(r"\W+", "", a.casefold()), re.sub(r"\W+", "", b.casefold())
    return bool(na) and (na == nb or (min(len(na), len(nb)) >= 6 and (na in nb or nb in na)))


def _clean(items: list[str] | str | None) -> list[str]:
    if items is None:
        return []
    if isinstance(items, str):
        items = [line for line in items.splitlines()]
    return [re.sub(r"^\s*[-*]\s*", "", i).strip() for i in items if i and i.strip()]


def apply_handoff(now_doc: Now, done=None, next_steps=None, blockers=None,
                  goal=None, in_progress=None) -> Now:
    keep_blockers = blockers is None
    done, next_steps, blockers = _clean(done), _clean(next_steps), _clean(blockers)
    in_progress = _clean(in_progress)
    sections = now_doc.sections
    stamp = now().strftime("%m/%d")
    if goal:
        sections["목표"] = _clean(goal)[:3]
    if done:
        completed = [f"({stamp}) {item}" for item in done]
        sections["최근 완료"] = (completed + sections.get("최근 완료", []))[:6]
        for name in ("진행 중", "다음"):
            sections[name] = [i for i in sections.get(name, []) if not any(_similar(i, d) for d in done)]
    if in_progress:
        sections["진행 중"] = in_progress[:6]
    if next_steps:
        sections["다음"] = next_steps[:8]
    if not keep_blockers:
        sections["막힘"] = blockers[:5]
    now_doc.updated = now().isoformat()
    _fit(now_doc)
    return now_doc


def record_decision(now_doc: Now, title: str) -> Now:
    items = [i for i in now_doc.sections.get("최근 결정", []) if not _similar(i, title)]
    now_doc.sections["최근 결정"] = ([title] + items)[:3]
    now_doc.updated = now().isoformat()
    _fit(now_doc)
    return now_doc


def drop_decision(now_doc: Now, title: str) -> bool:
    items = now_doc.sections.get("최근 결정") or []
    kept = [i for i in items if not _similar(i, title)]
    if kept == items:
        return False
    now_doc.sections["최근 결정"] = kept
    now_doc.updated = now().isoformat()
    return True


def _fit(now_doc: Now) -> None:
    order = ("최근 완료", "최근 결정", "진행 중", "다음", "막힘", "목표")
    while len(now_doc.text()) > NOW_LIMIT:
        for name in order:
            if len(now_doc.sections.get(name) or []) > 1:
                now_doc.sections[name].pop()
                break
        else:
            for name in order:
                items = now_doc.sections.get(name) or []
                if items and len(items[0]) > 80:
                    items[0] = items[0][:77] + "…"
                    break
            else:
                return


def save_now(hub: Hub, now_doc: Now) -> Path:
    path = hub.now_path(now_doc.project)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(now_doc.render(), encoding="utf-8")
    os.replace(tmp, path)
    return path
