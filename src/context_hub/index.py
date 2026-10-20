"""Derived SQLite index over knowledge pages. Safe to delete; rebuilt on demand."""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from . import knowledge
from .hub import Hub
from .text import features

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages(
  id TEXT PRIMARY KEY, path TEXT UNIQUE, project TEXT, type TEXT, status TEXT,
  title TEXT, body TEXT, updated TEXT, done INTEGER, sources TEXT, mtime REAL);
CREATE TABLE IF NOT EXISTS edges(src TEXT, dst TEXT, type TEXT);
CREATE INDEX IF NOT EXISTS edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst);
CREATE INDEX IF NOT EXISTS pages_project ON pages(project);
"""
QUERY_WEIGHT_CAP = 6.0
STATUS_WEIGHT = {"active": 1.0, "proposed": 0.65, "disputed": 0.5}


@dataclass
class Row:
    id: str
    path: str
    project: str
    type: str
    status: str
    title: str
    body: str
    updated: str
    done: bool
    sources: list[str]


class Index:
    def __init__(self, hub: Hub):
        self.hub = hub
        hub.index_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.conn = self._open()
        except sqlite3.DatabaseError:
            hub.index_path.unlink(missing_ok=True)
            self.conn = self._open()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.hub.index_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        return conn

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Index":
        self.sync()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def sync(self) -> int:
        """Bring the index up to date with files on disk. Returns pages changed."""
        on_disk: dict[str, float] = {}
        if self.hub.knowledge.exists():
            for path in self.hub.knowledge.rglob("*.md"):
                if path.name != "NOW.md":
                    try:
                        on_disk[str(path)] = path.stat().st_mtime
                    except OSError:
                        pass
        known = {r["path"]: r["mtime"] for r in self.conn.execute("SELECT path, mtime FROM pages")}
        changed = 0
        with self.conn:
            for path in set(known) - set(on_disk):
                self._remove_path(path)
                changed += 1
            for path, mtime in on_disk.items():
                if known.get(path) == mtime:
                    continue
                self._remove_path(path)
                try:
                    page = knowledge.load(Path(path))
                except Exception as exc:  # unreadable pages are skipped, not fatal
                    self.hub.log(f"index skip {path}: {exc}")
                    continue
                if not page.meta.get("id"):
                    continue
                self._insert(page, mtime)
                changed += 1
        return changed

    def _remove_path(self, path: str) -> None:
        row = self.conn.execute("SELECT id FROM pages WHERE path=?", (path,)).fetchone()
        if row:
            self.conn.execute("DELETE FROM edges WHERE src=?", (row["id"],))
        self.conn.execute("DELETE FROM pages WHERE path=?", (path,))

    def _insert(self, page: knowledge.Page, mtime: float) -> None:
        self.conn.execute("DELETE FROM edges WHERE src=?", (page.id,))
        self.conn.execute("DELETE FROM pages WHERE id=?", (page.id,))
        self.conn.execute(
            "INSERT INTO pages VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (page.id, str(page.path), page.project, page.type, page.status, page.title,
             page.body, str(page.meta.get("updated") or ""), int(page.done),
             json.dumps(page.sources), mtime),
        )
        edges = [(page.id, dst, kind) for kind, dsts in page.links.items() for dst in dsts]
        edges += [(page.id, dst, "related") for dst in page.wikilinks()]
        self.conn.executemany("INSERT INTO edges VALUES (?,?,?)", edges)

    def rebuild(self) -> int:
        with self.conn:
            self.conn.execute("DELETE FROM pages")
            self.conn.execute("DELETE FROM edges")
        return self.sync()

    # queries ---------------------------------------------------------------
    def _row(self, r: sqlite3.Row) -> Row:
        return Row(r["id"], r["path"], r["project"], r["type"], r["status"], r["title"],
                   r["body"], r["updated"], bool(r["done"]), json.loads(r["sources"] or "[]"))

    def get(self, page_id: str) -> Row | None:
        r = self.conn.execute("SELECT * FROM pages WHERE id=?", (page_id,)).fetchone()
        return self._row(r) if r else None

    def pages(self, projects: Iterable[str] | None = None, statuses: Iterable[str] | None = None) -> list[Row]:
        sql, args = "SELECT * FROM pages WHERE 1=1", []
        if projects is not None:
            projects = list(projects)
            sql += f" AND project IN ({','.join('?' * len(projects))})"
            args += projects
        if statuses is not None:
            statuses = list(statuses)
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += statuses
        return [self._row(r) for r in self.conn.execute(sql + " ORDER BY updated DESC, mtime DESC, id", args)]

    def edges_from(self, page_id: str) -> list[tuple[str, str]]:
        return [(r["dst"], r["type"]) for r in self.conn.execute("SELECT dst, type FROM edges WHERE src=?", (page_id,))]

    def edges_to(self, page_id: str) -> list[tuple[str, str]]:
        return [(r["src"], r["type"]) for r in self.conn.execute("SELECT src, type FROM edges WHERE dst=?", (page_id,))]

    def search(self, query: str, projects: Iterable[str] | None, limit: int = 12,
               statuses=("active", "proposed", "disputed")) -> list[tuple[float, Row]]:
        rows = self.pages(projects, statuses)
        q = list(dict.fromkeys(features(query)))
        if not q or not rows:
            return []
        docs = [(row, set(features(row.title)), features(row.body)) for row in rows]
        n = len(docs)
        df = {f: sum(1 for _, t, b in docs if f in t or f in b) for f in q}
        # normalise so scores mean "how much of the question this page covers" (~0..3)
        # Capped so long prompts full of instructions are not diluted: matching ~4 key words is enough.
        full = sum((0.5 if len(f) == 2 and "가" <= f[0] <= "힣" else 1.0) * math.log(1 + n) for f in q)
        ceiling = min(full, QUERY_WEIGHT_CAP * math.log(1 + n)) or 1.0
        scored = []
        for row, title_f, body_f in docs:
            body_set = set(body_f)
            score = 0.0
            for f in q:
                if not df[f]:
                    continue
                idf = math.log(1 + n / df[f])
                weight = 0.5 if len(f) == 2 and "가" <= f[0] <= "힣" else 1.0
                if f in title_f:
                    score += 2.0 * idf * weight
                if f in body_set:
                    score += min(body_f.count(f), 3) / 3 * idf * weight
            if score > 0:
                score = score / ceiling * STATUS_WEIGHT.get(row.status, 0.5)
                scored.append((score, row))
        scored.sort(key=lambda item: (-item[0], item[1].id))
        return scored[:limit]
