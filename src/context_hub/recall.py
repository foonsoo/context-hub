"""Assemble a small, cited context block for an agent."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import knowledge
from .hub import Hub
from .identity import Resolution, resolve
from .index import Index, Row
from .text import STATUS_WORDS, TASK_WORDS, estimate_tokens, has_any

DEFAULT_BUDGET = 2000
SNIPPET = 280


@dataclass
class RecallResult:
    text: str
    resolution: Resolution
    item_ids: list[str] = field(default_factory=list)
    best_score: float = 0.0


def _snippet(body: str, limit: int = SNIPPET) -> str:
    flat = " ".join(line.strip() for line in body.splitlines() if line.strip())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _source_label(row: Row) -> str:
    if not row.sources:
        return ""
    first = row.sources[0]
    more = f" 외 {len(row.sources) - 1}" if len(row.sources) > 1 else ""
    return f" · 출처: {first}{more}"


def _item(row: Row, extra: str = "", snippet_limit: int = SNIPPET) -> str:
    kind = "할 일" if row.type == "task" else row.type
    if row.type == "task" and row.done:
        kind = "완료"
    body = knowledge.strip_prefix(" ".join(row.body.split()))
    title = row.title.rstrip("…")
    if row.title.endswith("…") and body.startswith(title):
        # the title is just a cut-off copy of the body: show the body once
        line = f"- [{row.status} · {kind}] {_snippet(body, snippet_limit + 60)}"
    else:
        line = f"- [{row.status} · {kind}] {row.title}"
        if body.startswith(title):
            body = body[len(title):].lstrip(" .:—-")
        snippet = _snippet(body, snippet_limit) if body else ""
        if snippet:
            line += f" — {snippet}"
    line += f" ({row.updated}{_source_label(row)} · id:{row.id})"
    return line + extra


def _header(hub: Hub, res: Resolution, projects: dict) -> str:
    def label(slug: str) -> str:
        if slug == "global":
            return "전역"
        p = projects.get(slug)
        return f"{p.display} ({slug})" if p and p.display != slug else slug

    if res.all_projects:
        return "[프로젝트: 전체]"
    if res.slugs:
        names = ", ".join(label(s) for s in res.slugs)
        line = f"[프로젝트: {names}"
        if res.cwd_slug and res.cwd_slug not in res.slugs:
            line += f" · 현재 폴더는 {label(res.cwd_slug)}"
        if res.how == "text":
            line += " · 질문에 나온 이름으로 판단"
        return line + "]"
    return "[프로젝트: 없음 · 전역 지식만 검색]"


def _notices(res: Resolution, projects: dict) -> list[str]:
    out = []
    if res.unknown:
        known = ", ".join(sorted(projects)) or "없음"
        out.append(f"! '{res.unknown}' 프로젝트를 찾지 못했습니다. 등록된 프로젝트: {known}")
    for group in res.ambiguous:
        out.append(
            "! 이름이 여러 프로젝트에 해당합니다: " + ", ".join(group)
            + ". 사용자에게 어느 프로젝트인지 확인하세요."
        )
    if not res.slugs and not res.unknown and not res.ambiguous and projects:
        out.append("· 질문과 현재 폴더에서 프로젝트를 찾지 못했습니다. 등록된 프로젝트: " + ", ".join(sorted(projects)))
    return out


def _now_block(hub: Hub, slug: str, projects: dict, budget: int) -> str:
    title = projects[slug].display if slug in projects else slug
    now_doc = knowledge.load_now(hub, slug, title)
    if now_doc.is_empty():
        return ""
    text = now_doc.text().strip()
    if now_doc.updated:
        text = text.replace(f"# NOW · {now_doc.title}", f"## 진행 상태 · {now_doc.title} (NOW.md, {now_doc.updated[:10]} 갱신)", 1)
    while estimate_tokens(text) > budget and "\n" in text:
        text = text.rsplit("\n", 1)[0]
    return text


def recall(hub: Hub, query: str, cwd: str | None = None, project: str | None = None,
           budget: int = DEFAULT_BUDGET, include_now: bool = True) -> RecallResult:
    budget = max(200, min(int(budget or DEFAULT_BUDGET), 20000))
    projects = hub.projects()
    res = resolve(hub, cwd=cwd, text=query, project=project)
    lines = [_header(hub, res, projects), *_notices(res, projects)]
    used = estimate_tokens("\n".join(lines))
    ids: list[str] = []
    best = 0.0

    def add(block: str) -> bool:
        nonlocal used
        cost = estimate_tokens(block) + 1
        if used + cost > budget:
            return False
        lines.append(block)
        used += cost
        return True

    scope = list(res.slugs)
    if res.all_projects:
        for slug in scope:
            block = _now_block(hub, slug, projects, max(80, budget // max(len(scope), 1)))
            if block and not add(block):
                break
        scope = scope + ["global"]
    elif include_now:
        share = budget * 2 // 5 // max(len(scope), 1)
        for slug in scope:
            if slug != "global":
                block = _now_block(hub, slug, projects, share)
                if block:
                    add(block)

    search_scope = (scope + ["global"]) if scope else ["global"]
    wants_tasks = has_any(query, TASK_WORDS)
    with Index(hub) as index:
        hits = index.search(query, search_scope, limit=24)
        if wants_tasks or (has_any(query, STATUS_WORDS) and res.slugs):
            tasks = [r for r in index.pages([s for s in scope if s != "global"] or ["global"], ("active", "proposed"))
                     if r.type == "task" and not r.done]
            if tasks:
                add(f"## 열린 할 일 ({len(tasks)})")
                for row in tasks[:12]:
                    if add(_item(row, snippet_limit=120)):
                        ids.append(row.id)
        if has_any(query, STATUS_WORDS) and res.slugs and not res.all_projects:
            recent = [r for r in index.pages([s for s in scope if s != "global"], ("active", "proposed", "disputed"))
                      if r.type != "task"][:5]
            if recent:
                add("## 최근 기록")
                for row in recent:
                    if add(_item(row, snippet_limit=160)):
                        ids.append(row.id)
        if hits:
            best = hits[0][0]
            cutoff = best * 0.25
            hits = [(s, r) for s, r in hits if s >= cutoff and r.id not in ids]
        expanded: list[tuple[Row, str]] = []
        seen = set(ids)
        for score, row in hits:
            if row.id in seen:
                continue
            seen.add(row.id)
            extra = ""
            older = [index.get(dst) for dst, kind in index.edges_from(row.id) if kind == "supersedes"]
            older = [o for o in older if o]
            if older:
                extra = " ↳ 이전 결정: " + "; ".join(f"{o.title} (id:{o.id})" for o in older)
            expanded.append((row, extra))
            for dst, kind in index.edges_from(row.id) + index.edges_to(row.id):
                if kind in ("depends_on", "about", "contradicts") and dst not in seen:
                    neighbor = index.get(dst)
                    if neighbor and neighbor.status in ("active", "proposed", "disputed"):
                        seen.add(dst)
                        expanded.append((neighbor, f" ↳ 연결: {kind} ← {row.id}"))
        if expanded:
            add("## 관련 지식")
            for row, extra in expanded:
                if not add(_item(row, extra)):
                    lines.append("… (예산 초과로 생략)")
                    break
                ids.append(row.id)
    if len(lines) == 1 + len(_notices(res, projects)):
        lines.append("관련 지식이 아직 없습니다. 결정·제약·검증된 사실이 생기면 note로 남겨 주세요.")
    return RecallResult(text="\n".join(lines), resolution=res, item_ids=ids, best_score=best)
