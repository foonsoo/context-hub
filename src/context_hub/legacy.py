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
from .identity import _never_a_project, canonical_path, project_for_cwd, repo_info

DEFAULT_STATUSES = ("active",)


@dataclass
class Plan:
    mapping: dict[str, str] = field(default_factory=dict)  # legacy project id -> slug | "global"
    labels: dict[str, str] = field(default_factory=dict)  # legacy project id -> legacy slug
    new_projects: dict[str, Project] = field(default_factory=dict)
    counts: Counter = field(default_factory=Counter)
    skipped: Counter = field(default_factory=Counter)
    statuses: tuple[str, ...] = DEFAULT_STATUSES
    notes: dict[str, str] = field(default_factory=dict)
    extra_paths: dict[str, list[str]] = field(default_factory=dict)  # merged folders per slug
    renamed: dict[str, str] = field(default_factory=dict)

    def describe(self) -> str:
        lines = ["기존 프로젝트 → 새 프로젝트"]
        for legacy_id, target in sorted(self.mapping.items(), key=lambda kv: (kv[1], self.labels[kv[0]])):
            note = self.notes.get(legacy_id) or ("새로 등록" if target in self.new_projects else "")
            note = f" ({note})" if note else ""
            lines.append(f"  {self.labels[legacy_id]:<40} → {target:<24} [{self.counts[legacy_id]}건]{note}")
        if self.skipped:
            lines.append("가져오지 않음: " + ", ".join(f"{k} {v}건" for k, v in sorted(self.skipped.items())))
        return "\n".join(lines)


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _same_name(path: str, project: Project) -> bool:
    name = Path(path).name.casefold()
    labels = {project.slug.casefold(), project.name.casefold()}
    labels |= {Path(p).name.casefold() for p in project.paths}
    labels |= {r.rsplit("/", 1)[-1] for r in project.remotes}
    return name in labels


def plan(hub: Hub, db: Path, statuses: tuple[str, ...] = DEFAULT_STATUSES,
         merge_by_name: bool = True) -> Plan:
    """Map legacy projects onto hub projects without writing anything.

    Git repositories are matched by remote first so they set the canonical
    names; plain folders with the same name (copies made by ChatGPT/Codex
    workspaces) are then merged into them. Projects without anything to
    import are not registered.
    """
    result = Plan(statuses=statuses)
    projects = hub.projects()
    with _connect(db) as conn:
        for row in conn.execute("SELECT project_id, status, count(*) n FROM memories GROUP BY project_id, status"):
            if row["status"] in result.statuses:
                result.counts[row["project_id"]] += row["n"]
            else:
                result.skipped[row["status"]] += row["n"]
        paths: dict[str, list[str]] = {}
        for row in conn.execute("SELECT project_id, path FROM scopes WHERE path IS NOT NULL"):
            paths.setdefault(row["project_id"], []).append(canonical_path(row["path"]))
        rows = list(conn.execute("SELECT id, slug, name FROM projects ORDER BY created_at"))
    infos = {p: repo_info(p) for ps in paths.values() for p in ps}
    rows.sort(key=lambda r: not any(infos.get(p) for p in paths.get(r["id"], [])))
    for row in rows:
        result.labels[row["id"]] = row["slug"]
        target = None
        for path in paths.get(row["id"], []):
            if _never_a_project(path):
                target = "global"
                break
            known = {**projects, **result.new_projects}
            slug, _ = project_for_cwd(hub, path, known)
            if slug:
                target = slug
                break
            info = infos.get(path)
            if info and info.remote:
                match = next((p.slug for p in known.values() if info.remote in p.remotes), None)
                if match:
                    target = match
                    break
                base = info.remote.rsplit("/", 1)[-1]
                project = Project(slug=_unique(slugify(base), projects, result), name=base,
                                  remotes=[info.remote], paths=[info.common_root])
            else:
                if merge_by_name:
                    match = next((p.slug for p in known.values() if _same_name(path, p)), None)
                    if match:
                        target = match
                        result.notes[row["id"]] = "같은 이름 폴더라 합침"
                        if os.path.isdir(path):
                            result.extra_paths.setdefault(match, []).append(path)
                        break
                project = Project(slug=_unique(slugify(Path(path).name or row["slug"]), projects, result),
                                  name=Path(path).name or row["name"] or row["slug"],
                                  paths=[info.common_root if info else path])
            result.new_projects[project.slug] = project
            target = project.slug
            break
        result.mapping[row["id"]] = target or "global"
    used = Counter()
    for legacy_id, target in result.mapping.items():
        used[target] += result.counts[legacy_id]
    for slug in [s for s in result.new_projects if not used[s]]:
        del result.new_projects[slug]
        for legacy_id, target in result.mapping.items():
            if target == slug:
                result.notes[legacy_id] = "가져올 기억이 없어 등록하지 않음"
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
        original = project.slug
        created = hub.add_project(project)
        if created.slug != original:
            the_plan.renamed[original] = created.slug
            for key, value in the_plan.mapping.items():
                if value == original:
                    the_plan.mapping[key] = created.slug
    registered = hub.projects()
    for slug, paths in the_plan.extra_paths.items():
        slug = the_plan.renamed.get(slug, slug)
        if slug in registered:
            merged = registered[slug].paths + [p for p in paths if p not in registered[slug].paths]
            if merged != registered[slug].paths:
                hub.update_project(slug, paths=merged)
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
        page.meta["updated"] = (row["updated_at"] or row["created_at"] or "")[:10] or page.meta["updated"]
        page.meta["legacy_id"] = row["id"]
        if kind == "task" and row["status"] == "active":
            # context-memory used "task" for progress snapshots; keep them searchable, not open
            page.meta["done"] = True
        if row["status"] == "active":
            page.meta["reviewed_by"] = "legacy"
        knowledge.save(hub, page, touch=False)
        if row["status"] == "proposed":
            review.add(hub, "new", page_id=page.id, project=project, summary=f"[{kind}] {page.title} (가져옴)")
        imported_marker.append(row["id"])
        imported += 1
    hub.write_state("legacy", {"imported": imported_marker})
    hub.commit(f"import: {imported} memories from context-memory")
    return imported
