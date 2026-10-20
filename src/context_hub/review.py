"""Review queue: every change that would alter trusted knowledge waits here."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from . import knowledge
from .hub import Hub, now
from .inbox import new_id

KINDS = ("new", "append", "supersede", "dispute", "alias")


@dataclass
class Item:
    data: dict[str, Any]

    def __getattr__(self, key: str) -> Any:
        try:
            return self.data[key]
        except KeyError as exc:
            raise AttributeError(key) from exc


def add(hub: Hub, kind: str, *, page_id: str | None = None, target_id: str | None = None,
        project: str | None = None, summary: str = "", content: str = "",
        sources: list[str] | None = None, extra: dict | None = None) -> Item:
    assert kind in KINDS, kind
    hub.review_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "id": new_id("rev"), "kind": kind, "created": now().isoformat(),
        "project": project, "page_id": page_id, "target_id": target_id,
        "summary": summary, "content": content, "sources": sources or [],
        **(extra or {}),
    }
    path = hub.review_dir / f"{data['id']}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return Item(data)


def pending(hub: Hub, project: str | None = None) -> list[Item]:
    items = []
    if not hub.review_dir.exists():
        return items
    for path in sorted(hub.review_dir.glob("rev-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if project and data.get("project") != project:
            continue
        items.append(Item(data))
    items.sort(key=lambda i: i.data.get("created", ""))
    return items


def get(hub: Hub, item_id: str) -> Item | None:
    path = hub.review_dir / f"{item_id}.json"
    if not path.exists():
        return None
    return Item(json.loads(path.read_text(encoding="utf-8")))


def _close(hub: Hub, item: Item) -> None:
    (hub.review_dir / f"{item.id}.json").unlink(missing_ok=True)


def _page(hub: Hub, page_id: str | None) -> knowledge.Page | None:
    return knowledge.find(hub, page_id) if page_id else None


def describe(hub: Hub, item: Item) -> str:
    page = _page(hub, item.data.get("page_id"))
    target = _page(hub, item.data.get("target_id"))
    lines = [f"{item.id} · {item.kind} · 프로젝트: {item.data.get('project') or 'global'}"]
    if item.kind == "new" and page:
        lines += [f"  새 지식 [{page.type}] {page.title}", _indent(page.body)]
    elif item.kind == "append" and target:
        lines += [f"  기존 지식에 추가: {target.title} (id:{target.id})", _indent(item.content)]
    elif item.kind == "supersede" and page and target:
        lines += [f"  대체: {target.title} (id:{target.id})", f"  →  {page.title} (id:{page.id})", _indent(page.body)]
    elif item.kind == "dispute" and target:
        lines += [f"  모순 의심: {target.title} (id:{target.id})", _indent(item.content)]
    elif item.kind == "alias":
        lines += [f"  별칭 추가 제안: {item.data.get('alias')} → {item.data.get('project')}"]
    else:
        lines.append(f"  {item.summary} (대상 지식이 사라졌습니다)")
    if item.data.get("sources"):
        lines.append("  출처: " + ", ".join(item.data["sources"][:3]))
    elif page and page.sources:
        lines.append("  출처: " + ", ".join(page.sources[:3]))
    return "\n".join(lines)


def _indent(text: str, limit: int = 600) -> str:
    text = text.strip()
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return "\n".join("    " + line for line in text.splitlines())


def _update_now_decision(hub: Hub, page: knowledge.Page) -> None:
    if page.type != "decision" or page.project == "global":
        return
    projects = hub.projects()
    title = projects[page.project].display if page.project in projects else page.project
    with hub.lock(f"now-{page.project}"):
        doc = knowledge.load_now(hub, page.project, title)
        knowledge.record_decision(doc, page.title)
        knowledge.save_now(hub, doc)


def forget_in_now(hub: Hub, page: knowledge.Page) -> None:
    if page.project == "global" or not hub.now_path(page.project).exists():
        return
    with hub.lock(f"now-{page.project}"):
        doc = knowledge.load_now(hub, page.project)
        if knowledge.drop_decision(doc, page.title):
            knowledge.save_now(hub, doc)


def approve(hub: Hub, item: Item) -> str:
    kind = item.kind
    page = _page(hub, item.data.get("page_id"))
    target = _page(hub, item.data.get("target_id"))
    if kind == "new":
        if not page:
            _close(hub, item)
            return "대상 지식이 없어 항목을 닫았습니다."
        page.meta["status"] = "active"
        page.meta["reviewed_by"] = "user"
        knowledge.save(hub, page, by_curator=not page.human_edited())
        _update_now_decision(hub, page)
        message = f"승인: {page.title}"
    elif kind == "append":
        if not target:
            _close(hub, item)
            return "대상 지식이 없어 항목을 닫았습니다."
        target.body = target.body.rstrip() + "\n\n" + item.content.strip()
        target.meta["sources"] = target.sources + [s for s in item.data.get("sources", []) if s not in target.sources]
        knowledge.save(hub, target)
        message = f"추가 반영: {target.title}"
    elif kind == "supersede":
        if not page or not target:
            _close(hub, item)
            return "대상 지식이 없어 항목을 닫았습니다."
        page.meta["status"] = "active"
        page.meta["reviewed_by"] = "user"
        links = page.links
        links.setdefault("supersedes", [])
        if target.id not in links["supersedes"]:
            links["supersedes"].append(target.id)
        page.meta["links"] = links
        knowledge.save(hub, page, by_curator=not page.human_edited())
        target.meta["status"] = "superseded"
        target.meta["superseded_by"] = page.id
        knowledge.save(hub, target, by_curator=not target.human_edited())
        forget_in_now(hub, target)
        _update_now_decision(hub, page)
        message = f"대체 승인: {target.title} → {page.title}"
    elif kind == "dispute":
        if not target:
            _close(hub, item)
            return "대상 지식이 없어 항목을 닫았습니다."
        target.meta["status"] = "disputed"
        target.body = target.body.rstrip() + "\n\n> 모순 의심: " + item.content.strip()
        target.meta["sources"] = target.sources + item.data.get("sources", [])
        knowledge.save(hub, target)
        message = f"모순 표시: {target.title}"
    elif kind == "alias":
        slug, alias = item.data.get("project"), item.data.get("alias")
        projects = hub.projects()
        if slug in projects and alias and alias not in projects[slug].aliases:
            hub.update_project(slug, aliases=projects[slug].aliases + [alias])
        message = f"별칭 추가: {alias} → {slug}"
    else:
        message = "알 수 없는 항목"
    _close(hub, item)
    return message


def reject(hub: Hub, item: Item) -> str:
    page = _page(hub, item.data.get("page_id"))
    if item.kind in ("new", "supersede") and page and page.status == "proposed":
        page.meta["status"] = "archived"
        page.meta["rejected"] = True
        knowledge.save(hub, page, by_curator=not page.human_edited())
    _close(hub, item)
    return f"기각: {item.summary or item.id}"


def edit_then_approve(hub: Hub, item: Item, editor: str | None = None) -> str:
    """Open the page in $EDITOR, then approve with the edited body."""
    import subprocess

    page = _page(hub, item.data.get("page_id") or item.data.get("target_id"))
    if not page or not page.path:
        return "편집할 페이지가 없습니다."
    editor = editor or os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    subprocess.call([*editor.split(), str(page.path)])
    return approve(hub, item)
