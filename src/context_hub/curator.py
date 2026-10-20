"""Background curator: turns collected sessions into proposed knowledge.

The curator never activates knowledge on its own. New pages are ``proposed``;
changes to existing trusted pages go to the review queue. NOW.md is working
state and is updated directly (git keeps the history).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import inbox, knowledge, review
from .hub import Hub, today
from .identity import git_info, register_cwd
from .index import Index, Row
from .secrets import redact
from .text import jaccard

CHUNK_CHARS = 60_000
MIN_SESSION_CHARS = 400
QUIET_SECONDS = 600
DEFAULT_DAILY_CALLS = 40
DEFAULT_MODEL = "claude-opus-5-5"
RECENT_CONTEXT = 8

OPERATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "operations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["create", "append", "supersede", "dispute"]},
                    "type": {"type": "string", "enum": sorted(knowledge.TYPES)},
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "evidence_start": {"type": "integer"},
                    "evidence_end": {"type": "integer"},
                    "target_id": {"type": "string"},
                    "about": {"type": "array", "items": {"type": "string"}},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["op", "type", "title", "content", "evidence_start", "evidence_end",
                             "target_id", "about", "depends_on"],
                "additionalProperties": False,
            },
        },
        "now": {
            "type": "object",
            "properties": {
                "has_update": {"type": "boolean"},
                "goal": {"type": "string"},
                "in_progress": {"type": "array", "items": {"type": "string"}},
                "next": {"type": "array", "items": {"type": "string"}},
                "blockers": {"type": "array", "items": {"type": "string"}},
                "done": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["has_update", "goal", "in_progress", "next", "blockers", "done"],
            "additionalProperties": False,
        },
    },
    "required": ["operations", "now"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You maintain a knowledge base for a software developer who works with several AI coding agents.
You read one working session transcript at a time and decide what is worth remembering for future sessions.

Keep only durable, reusable knowledge: decisions and their reasons, constraints, verified facts about the
project, procedures that worked, the user's preferences, and tasks that remain open. Skip chit-chat,
transient debugging output, tool noise, and anything already captured unless the session changes it.

Rules:
- Every operation must cite the transcript lines that support it (evidence_start..evidence_end, inclusive,
  using the L numbers shown). Do not invent facts that the lines do not support.
- Prefer "append" or "supersede" over "create" when an existing page (listed under EXISTING) covers the topic.
  Use "supersede" only when the session clearly replaces an earlier decision; use "dispute" when the session
  contradicts an existing page without settling it. target_id must be an id from EXISTING, else "".
- about/depends_on may only contain ids from EXISTING.
- Write titles and content in the language the user used. Titles under 60 characters; content 1-6 sentences.
- Never copy secrets, tokens, passwords or personal data.
- For "now": set has_update true only if the session shows the current goal, work in progress, next steps,
  blockers or completed work for this project. Use empty strings/lists for unknown fields.
- Returning no operations is a valid, common outcome."""


class Extractor(Protocol):
    def extract(self, prompt: str) -> dict[str, Any]: ...


@dataclass
class CurateReport:
    sessions: int = 0
    calls: int = 0
    created: int = 0
    appended: int = 0
    queued: int = 0
    rejected_ops: int = 0
    now_updates: int = 0
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"세션 {self.sessions}개 처리", f"새 지식 {self.created}", f"추가 {self.appended}",
                 f"검토 대기 {self.queued}", f"NOW 갱신 {self.now_updates}"]
        if self.rejected_ops:
            parts.append(f"근거 부족으로 버린 제안 {self.rejected_ops}")
        if self.errors:
            parts.append(f"오류 {len(self.errors)}")
        return ", ".join(parts)


class AnthropicExtractor:
    """Calls the Claude API with structured outputs. Requires ``pip install context-hub[llm]``."""

    def __init__(self, model: str | None = None, effort: str | None = None):
        import anthropic  # noqa: F401  (import error surfaces as "LLM not available")

        self.anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model = model or os.environ.get("CTX_CURATOR_MODEL") or DEFAULT_MODEL
        self.effort = effort or os.environ.get("CTX_CURATOR_EFFORT") or "medium"

    def extract(self, prompt: str) -> dict[str, Any]:
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": OPERATION_SCHEMA},
            },
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("the model declined this session")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("response hit max_tokens")
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text)


def default_extractor() -> Extractor | None:
    if os.environ.get("CTX_CURATOR", "").casefold() in ("off", "0", "false", "none"):
        return None
    try:
        return AnthropicExtractor()
    except Exception:
        return None


# pending work ---------------------------------------------------------------

def _transcript_files(hub: Hub) -> list[Path]:
    return [p for p in inbox.files(hub) if p.name not in ("notes.jsonl",) and p.parent.name != "legacy"]


def pending(hub: Hub, quiet_seconds: float = QUIET_SECONDS) -> list[tuple[Path, int]]:
    state = hub.read_state("curator").get("files", {})
    result = []
    for path in _transcript_files(hub):
        rel = path.relative_to(hub.root).as_posix()
        done = int(state.get(rel, 0))
        with open(path, "rb") as handle:
            total = sum(1 for _ in handle)
        if total <= done:
            continue
        if quiet_seconds and time.time() - path.stat().st_mtime < quiet_seconds:
            continue
        result.append((path, done))
    return result


def _mark(hub: Hub, path: Path, lines: int) -> None:
    state = hub.read_state("curator")
    state.setdefault("files", {})[path.relative_to(hub.root).as_posix()] = lines
    hub.write_state("curator", state)


def _budget_ok(hub: Hub) -> bool:
    limit = int(os.environ.get("CTX_CURATOR_DAILY_CALLS", DEFAULT_DAILY_CALLS))
    usage = hub.read_state("curator").get("usage", {})
    return usage.get(today(), 0) < limit


def _count_call(hub: Hub) -> None:
    state = hub.read_state("curator")
    usage = state.setdefault("usage", {})
    usage[today()] = usage.get(today(), 0) + 1
    for day in sorted(usage)[:-14]:
        usage.pop(day)
    hub.write_state("curator", state)


def _chunks(entries: list[inbox.Entry]) -> list[list[inbox.Entry]]:
    chunks, current, size = [], [], 0
    for entry in entries:
        length = len(entry["text"] or "") + 40
        if current and size + length > CHUNK_CHARS:
            chunks.append(current)
            current, size = [], 0
        current.append(entry)
        size += length
    if current:
        chunks.append(current)
    return chunks


def _line_no(entry: inbox.Entry) -> int:
    return int(entry.ref.rsplit("#L", 1)[1])


def build_prompt(project: str, project_name: str, entries: list[inbox.Entry],
                 existing: list[Row], now_text: str) -> str:
    lines = [f"PROJECT: {project_name} ({project})", "", "EXISTING (id · status · type · title — summary):"]
    for row in existing:
        summary = " ".join(row.body.split())[:200]
        lines.append(f"- {row.id} · {row.status} · {row.type} · {row.title} — {summary}")
    if not existing:
        lines.append("- (none)")
    lines += ["", "CURRENT NOW.md:", now_text.strip() or "(empty)", "", "TRANSCRIPT:"]
    for entry in entries:
        text = " ".join((entry["text"] or "").split())
        lines.append(f"L{_line_no(entry)} {entry['role']}: {text}")
    return "\n".join(lines)


def _valid_ids(ids: list[str], allowed: set[str]) -> list[str]:
    return [i for i in ids if i in allowed]


def apply_operations(hub: Hub, project: str, file: Path, entries: list[inbox.Entry],
                     result: dict[str, Any], existing: list[Row], report: CurateReport) -> None:
    rel = file.relative_to(hub.root).as_posix()
    line_numbers = {_line_no(e) for e in entries}
    first, last = min(line_numbers), max(line_numbers)
    allowed = {row.id for row in existing}
    for op in result.get("operations") or []:
        try:
            start, end = int(op["evidence_start"]), int(op["evidence_end"])
            kind = knowledge.normalize_type(op.get("type"))
            title = redact(str(op.get("title") or "")).strip()
            content = redact(str(op.get("content") or "")).strip()[:2000]
            action = op.get("op")
            target_id = op.get("target_id") or ""
        except (KeyError, TypeError, ValueError):
            report.rejected_ops += 1
            continue
        if not (first <= start <= end <= last) or not kind or not title or not content:
            report.rejected_ops += 1
            continue
        if action in ("append", "supersede", "dispute") and target_id not in allowed:
            report.rejected_ops += 1
            continue
        source = f"{rel}#L{start}" + (f"-L{end}" if end != start else "")
        links = {
            key: _valid_ids(list(op.get(key) or []), allowed)
            for key in ("about", "depends_on")
        }
        links = {k: v for k, v in links.items() if v}
        if action == "create":
            duplicate = next(
                (row for row in existing if row.type == kind and jaccard(row.body, content) >= 0.75), None
            )
            if duplicate:
                action, target_id = "append", duplicate.id
        if action == "create":
            page = knowledge.new_page(kind, title, content, project, [source], links=links)
            knowledge.save(hub, page)
            review.add(hub, "new", page_id=page.id, project=project, summary=f"[{kind}] {title}")
            report.created += 1
        elif action == "append":
            target = knowledge.find(hub, target_id)
            if not target:
                report.rejected_ops += 1
                continue
            if target.status == "proposed" and not target.human_edited():
                target.body = target.body.rstrip() + "\n\n" + content
                target.meta["sources"] = target.sources + [source]
                knowledge.save(hub, target)
                report.appended += 1
            else:
                review.add(hub, "append", target_id=target.id, project=project,
                           summary=f"{target.title}에 추가", content=content, sources=[source])
                report.queued += 1
        elif action == "supersede":
            links["supersedes"] = [target_id]
            page = knowledge.new_page(kind, title, content, project, [source], links=links)
            knowledge.save(hub, page)
            review.add(hub, "supersede", page_id=page.id, target_id=target_id, project=project,
                       summary=f"{title} (대체)")
            report.queued += 1
        elif action == "dispute":
            review.add(hub, "dispute", target_id=target_id, project=project,
                       summary=f"모순 의심: {title}", content=content, sources=[source])
            report.queued += 1
        else:
            report.rejected_ops += 1
    now_update = result.get("now") or {}
    if now_update.get("has_update") and project != "global":
        projects = hub.projects()
        title = projects[project].display if project in projects else project
        with hub.lock(f"now-{project}"):
            doc = knowledge.load_now(hub, project, title)
            knowledge.apply_handoff(
                doc,
                done=[redact(x) for x in now_update.get("done") or []],
                next_steps=[redact(x) for x in now_update.get("next") or []],
                blockers=[redact(x) for x in now_update.get("blockers") or []] or None,
                goal=redact(now_update.get("goal") or "") or None,
                in_progress=[redact(x) for x in now_update.get("in_progress") or []],
            )
            knowledge.save_now(hub, doc)
        report.now_updates += 1


def _session_project(hub: Hub, entries: list[inbox.Entry]) -> str | None:
    project = next((e["project"] for e in entries if e["project"]), None)
    if project:
        return project
    cwd = next((e["cwd"] for e in entries if e["cwd"]), None)
    if cwd and os.path.isdir(cwd) and git_info(cwd):
        return register_cwd(hub, cwd)  # only git repositories become projects automatically
    return None


def curate(hub: Hub, extractor: Extractor | None = None, quiet_seconds: float = QUIET_SECONDS,
           max_sessions: int | None = None) -> CurateReport:
    report = CurateReport()
    if extractor is None:
        report.skipped.append("LLM 큐레이터가 설정되지 않아 대화 기록 정리는 건너뜁니다 (note/handoff는 즉시 반영됨).")
        return report
    with hub.lock("curator"):
        work = pending(hub, quiet_seconds)
        if max_sessions is not None:
            work = work[:max_sessions]
        projects = hub.projects()
        for path, done in work:
            with open(path, "rb") as handle:
                total = sum(1 for _ in handle)
            entries = [e for e in inbox.read_lines(hub, path, start=done) if _line_no(e) <= total]
            useful = [e for e in entries if e["role"] in ("user", "assistant", "tool")]
            user_text = sum(len(e["text"] or "") for e in useful if e["role"] in ("user", "assistant"))
            project = _session_project(hub, useful)
            if not useful or user_text < MIN_SESSION_CHARS or not project:
                _mark(hub, path, total)
                continue
            if project in projects and not projects[project].capture:
                _mark(hub, path, total)
                continue
            report.sessions += 1
            failed = False
            for chunk in _chunks(useful):
                if not _budget_ok(hub):
                    report.skipped.append("오늘 큐레이터 호출 한도에 도달했습니다. 남은 세션은 다음에 처리합니다.")
                    return report
                with Index(hub) as index:
                    query = " ".join((e["text"] or "")[:200] for e in chunk if e["role"] == "user")[:4000]
                    existing = [row for _, row in index.search(query, [project, "global"], limit=12)]
                    recent = index.pages([project], ("active", "proposed", "disputed"))[:RECENT_CONTEXT]
                    existing += [row for row in recent if row.id not in {e.id for e in existing}]
                name = projects[project].display if project in projects else project
                now_text = knowledge.load_now(hub, project, name).text()
                prompt = build_prompt(project, name, chunk, existing, now_text)
                try:
                    _count_call(hub)
                    report.calls += 1
                    result = extractor.extract(prompt)
                except Exception as exc:
                    report.errors.append(f"{path.name}: {exc}")
                    hub.log(f"curator error {path}: {exc}")
                    failed = True
                    break
                apply_operations(hub, project, path, chunk, result, existing, report)
            if not failed:
                _mark(hub, path, total)
        hub.commit(f"curator: {report.summary()}")
    return report
