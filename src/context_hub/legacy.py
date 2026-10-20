"""Import knowledge from the previous context-memory SQLite database (read-only)."""

from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import inbox, knowledge, review
from .hub import Hub, Project, slugify
from .identity import _never_a_project, canonical_path, git_info, project_for_cwd

DEFAULT_STATUSES = ("active",)


@dataclass
class Plan:
    mapping: dict[str, str] = field(default_factory=dict)  # legacy project id -> slug | "global"
    labels: dict[str, str] = field(default_factory=dict)  # legacy project id -> legacy slug
    new_projects: dict[str, Project] = field(default_factory=dict)
    counts: Counter = field(default_factory=Counter)
    skipped: Counter = field(default_factory=Counter)
    statuses: tuple[str, ...] = DEFAULT_STATUSES

    def describe(self) -> str:
        lines = ["기존 프로젝트 → 새 프로젝트"]
        for legacy_id, target in sorted(self.mapping.items(), key=lambda kv: self.labels[kv[0]]):
            marker = " (새로 등록)" if target in self.new_projects else ""
            lines.append(f"  {self.labels[legacy_id]:<40} → {target}{marker}  [{self.counts[legacy_id]}건]")
        if self.skipped:
            lines.append("가져오지 않음: " + ", ".join(f"{k} {v}건" for k, v in sorted(self.skipped.items())))
        return "\n".join(lines)


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def plan(hub: Hub, db: Path, statuses: tuple[str, ...] = DEFAULT_STATUSES) -> Plan:
    result = Plan(statuses=statuses)
    projects = hub.projects()
    with _connect(db) as conn:
        paths: dict[str, list[str]] = {}
        for row in conn.execute("SELECT project_id, path FROM scopes WHERE path IS NOT NULL"):
            paths.setdefault(row["project_id"], []).append(row["path"])
        for row in conn.execute("SELECT id, slug, name FROM projects ORDER BY created_at"):
            result.labels[row["id"]] = row["slug"]
            target = None
            for path in paths.get(row["id"], []):
                path = canonical_path(path)
                if _never_a_project(path):
                    target = "global"
                    break
                slug, _ = project_for_cwd(hub, path, {**projects, **result.new_projects})
                if slug:
                    target = slug
                    break
                info = git_info(path) if os.path.isdir(path) else None
                if info and info.remote:
                    match = next((p.slug for p in result.new_projects.values() if info.remote in p.remotes), None)
                    if match:
                        target = match
                        break
                    base = info.remote.rsplit("/", 1)[-1]
                    project = Project(slug=_unique(slugify(base), projects, result), name=base,
                                      remotes=[info.remote], paths=[info.common_root])
                else:
                    project = Project(slug=_unique(slugify(row["slug"]), projects, result),
                                      name=row["name"] or row["slug"], paths=[path])
                result.new_projects[project.slug] = project
                target = project.slug
                break
            result.mapping[row["id"]] = target or "global"
        for row in conn.execute("SELECT project_id, status, count(*) n FROM memories GROUP BY project_id, status"):
            if row["status"] in result.statuses:
                result.counts[row["project_id"]] += row["n"]
            else:
                result.skipped[row["status"]] += row["n"]
    return result


def _unique(slug: str, projects: dict, result: Plan) -> str:
    base, n = slug, 2
    while slug in projects or slug in result.new_projects:
        slug = f"{base}-{n}"
        n += 1
    return slug


def apply(hub: Hub, db: Path, the_plan: Plan) -> int:
    hub.ensure()
    for project in the_plan.new_projects.values():
        created = hub.add_project(project)
        if created.slug != project.slug:
            for key, value in the_plan.mapping.items():
                if value == project.slug:
                    the_plan.mapping[key] = created.slug
    imported_marker = hub.read_state("legacy").get("imported", [])
    imported = 0
    log_file = hub.inbox / "legacy" / "context-memory.jsonl"
    with _connect(db) as conn:
        sources: dict[str, list[str]] = {}
        for row in conn.execute(
            "SELECT ms.memory_id, e.id, e.kind, e.content, e.created_at FROM memory_sources ms "
            "JOIN events e ON e.id = ms.event_id ORDER BY e.event_seq"
        ):
            sources.setdefault(row["memory_id"], []).append(dict(row))
        marks = ",".join("?" * len(the_plan.statuses))
        rows = list(conn.execute(
            f"SELECT * FROM memories WHERE status IN ({marks}) ORDER BY created_at", the_plan.statuses,
        ))
    for row in rows:
        if row["id"] in imported_marker:
            continue
        project = the_plan.mapping.get(row["project_id"], "global")
        records = [
            {"kind": e["kind"], "text": e["content"], "legacy_event": e["id"], "ts": e["created_at"],
             "project": project, "source": "legacy"}
            for e in sources.get(row["id"], [])
        ] or [{"kind": row["type"], "text": row["content"], "legacy_memory": row["id"],
               "ts": row["created_at"], "project": project, "source": "legacy"}]
        refs = inbox.append(hub, log_file, records)
        kind = knowledge.normalize_type(row["type"]) or "fact"
        page = knowledge.new_page(kind, row["title"] or knowledge.title_from(row["content"]),
                                  row["content"] or "", project, refs, status=row["status"])
        page.meta["created"] = (row["created_at"] or "")[:10] or page.meta["created"]
        page.meta["legacy_id"] = row["id"]
        if row["status"] == "active":
            page.meta["reviewed_by"] = "legacy"
        knowledge.save(hub, page)
        if row["status"] == "proposed":
            review.add(hub, "new", page_id=page.id, project=project, summary=f"[{kind}] {page.title} (가져옴)")
        imported_marker.append(row["id"])
        imported += 1
    hub.write_state("legacy", {"imported": imported_marker})
    hub.commit(f"import: {imported} memories from context-memory")
    return imported
