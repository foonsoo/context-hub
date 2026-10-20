"""Write operations shared by the CLI, MCP server and hooks."""

from __future__ import annotations

from dataclasses import dataclass

from . import inbox, knowledge, review
from .hub import Hub
from .identity import register_cwd, resolve
from .index import Index
from .secrets import redact
from .text import jaccard

DUPLICATE_THRESHOLD = 0.8


@dataclass
class WriteResult:
    ok: bool
    message: str
    project: str | None = None
    page_id: str | None = None
    ref: str | None = None


def _target(hub: Hub, cwd: str | None, text: str, project: str | None, kind: str | None):
    res = resolve(hub, cwd=cwd, text=text, project=project, for_write=True)
    if res.unknown:
        return None, f"'{res.unknown}' 프로젝트를 찾지 못했습니다. `ctx projects`로 목록을 확인하세요."
    if res.ambiguous:
        groups = " / ".join(", ".join(g) for g in res.ambiguous)
        return None, f"대상 프로젝트가 모호해 기록하지 않았습니다 ({groups}). project 인자로 지정하세요."
    if res.slug:
        return res.slug, None
    if kind == "preference":
        return "global", None
    if cwd:
        slug = register_cwd(hub, cwd)
        if slug:
            return slug, None
    return "global", None


def note(hub: Hub, text: str, cwd: str | None = None, kind: str | None = None,
         project: str | None = None, client: str = "cli", session: str | None = None) -> WriteResult:
    text = redact((text or "").strip())
    if not text:
        return WriteResult(False, "내용이 비어 있습니다.")
    kind = knowledge.normalize_type(kind) or knowledge.guess_type(text)
    if kind == "preference" and not project:
        slug = "global"
    else:
        slug, error = _target(hub, cwd, text, project, kind)
        if error:
            return WriteResult(False, error)
    ref = inbox.add_note(hub, {
        "kind": kind, "text": text, "project": slug, "cwd": cwd,
        "client": client, "session": session, "source": "note",
    })
    with Index(hub) as index:
        for row in index.pages([slug], ("proposed", "active")):
            if row.type == kind and jaccard(row.body, text) >= DUPLICATE_THRESHOLD:
                page = knowledge.find(hub, row.id)
                if page:
                    page.meta["sources"] = page.sources + [ref]
                    knowledge.save(hub, page, by_curator=not page.human_edited())
                    hub.commit(f"note: {page.title}")
                    return WriteResult(True, f"이미 있는 지식에 출처를 더했습니다 (id:{page.id}, 프로젝트: {slug}).",
                                       slug, page.id, ref)
    page = knowledge.new_page(kind, knowledge.title_from(text), text, slug, [ref])
    knowledge.save(hub, page)
    review.add(hub, "new", page_id=page.id, project=slug, summary=f"[{kind}] {page.title}")
    hub.commit(f"note: {page.title}")
    return WriteResult(True, f"기록했습니다 (proposed, id:{page.id}, 프로젝트: {slug}).", slug, page.id, ref)


def handoff(hub: Hub, cwd: str | None = None, done=None, next_steps=None, blockers=None,
            goal=None, in_progress=None, project: str | None = None, client: str = "cli",
            session: str | None = None) -> WriteResult:
    payload = {
        "goal": goal, "done": done, "next": next_steps, "blockers": blockers,
        "in_progress": in_progress,
    }
    if not any(payload.values()):
        return WriteResult(False, "done/next/blockers 중 하나는 있어야 합니다.")
    summary = "\n".join(f"{k}: {v}" for k, v in payload.items() if v)
    slug, error = _target(hub, cwd, summary, project, None)
    if error:
        return WriteResult(False, error)
    if slug == "global":
        return WriteResult(False, "프로젝트를 정할 수 없어 handoff를 남기지 않았습니다. 프로젝트 폴더에서 실행하거나 project를 지정하세요.")
    ref = inbox.add_note(hub, {
        "kind": "handoff", "text": redact(summary), "project": slug, "cwd": cwd,
        "client": client, "session": session, "source": "handoff",
    })
    projects = hub.projects()
    title = projects[slug].display if slug in projects else slug
    with hub.lock(f"now-{slug}"):
        doc = knowledge.load_now(hub, slug, title)
        knowledge.apply_handoff(
            doc,
            done=[redact(x) for x in knowledge._clean(done)],
            next_steps=[redact(x) for x in knowledge._clean(next_steps)],
            blockers=[redact(x) for x in knowledge._clean(blockers)] if blockers is not None else None,
            goal=redact(goal) if isinstance(goal, str) else goal,
            in_progress=[redact(x) for x in knowledge._clean(in_progress)],
        )
        knowledge.save_now(hub, doc)
    hub.commit(f"handoff: {slug}")
    return WriteResult(True, f"NOW.md를 갱신했습니다 (프로젝트: {slug}).", slug, None, ref)
