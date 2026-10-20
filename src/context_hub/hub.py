"""The hub directory: layout, project registry, state files and git history."""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

GITIGNORE = ".index/\n.state/\n"
REGISTRY_HEADER = (
    "# Context Hub projects. Safe to edit by hand.\n"
    "# name/aliases are matched against questions such as \"B 프로젝트 상태 알려줘\".\n"
    "# remotes/paths decide which project a working directory belongs to.\n"
)
CURATOR_AUTHOR = ("context-hub", "context-hub@localhost")


def default_root() -> Path:
    return Path(os.environ.get("CTX_HOME") or Path.home() / "context-hub").expanduser()


def now() -> dt.datetime:
    fixed = os.environ.get("CTX_NOW")
    if fixed:
        return dt.datetime.fromisoformat(fixed)
    return dt.datetime.now().astimezone().replace(microsecond=0)


def today() -> str:
    return now().date().isoformat()


def slugify(text: str, limit: int = 48) -> str:
    slug = re.sub(r"[^0-9a-z가-힣._-]+", "-", text.casefold()).strip("-._")
    return slug[:limit].strip("-._") or "item"


@dataclass
class Project:
    slug: str
    name: str = ""
    aliases: list[str] = field(default_factory=list)
    remotes: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    capture: bool = True

    @property
    def display(self) -> str:
        return self.name or self.slug

    def labels(self) -> list[str]:
        return [label for label in [self.slug, self.name, *self.aliases] if label]


class Hub:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(root).expanduser() if root else default_root()

    # layout -------------------------------------------------------------
    @property
    def inbox(self) -> Path:
        return self.root / "inbox"

    @property
    def knowledge(self) -> Path:
        return self.root / "knowledge"

    @property
    def review_dir(self) -> Path:
        return self.root / "review"

    @property
    def registry_path(self) -> Path:
        return self.root / "projects.toml"

    @property
    def state_dir(self) -> Path:
        return self.root / ".state"

    @property
    def index_path(self) -> Path:
        return self.root / ".index" / "index.db"

    def exists(self) -> bool:
        return self.registry_path.exists()

    def ensure(self) -> "Hub":
        for path in (self.inbox, self.knowledge / "global", self.review_dir, self.state_dir):
            path.mkdir(parents=True, exist_ok=True)
        gitignore = self.root / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(GITIGNORE)
        if not self.registry_path.exists():
            self.registry_path.write_text(REGISTRY_HEADER)
        return self

    def project_dir(self, slug: str | None) -> Path:
        if not slug or slug == "global":
            return self.knowledge / "global"
        return self.knowledge / "projects" / slug

    def now_path(self, slug: str) -> Path:
        return self.project_dir(slug) / "NOW.md"

    # locking and state ---------------------------------------------------
    @contextlib.contextmanager
    def lock(self, name: str = "hub") -> Iterator[None]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with open(self.state_dir / f"{name}.lock", "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def read_state(self, name: str) -> dict[str, Any]:
        path = self.state_dir / f"{name}.json"
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def write_state(self, name: str, data: dict[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / f"{name}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        os.replace(tmp, path)

    def log(self, message: str) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with open(self.state_dir / "ctx.log", "a") as handle:
            handle.write(f"{now().isoformat()} {message}\n")

    # registry -------------------------------------------------------------
    def projects(self) -> dict[str, Project]:
        try:
            data = tomllib.loads(self.registry_path.read_text())
        except FileNotFoundError:
            return {}
        result = {}
        for slug, raw in (data.get("projects") or {}).items():
            if not isinstance(raw, dict):
                continue
            result[slug] = Project(
                slug=slug,
                name=str(raw.get("name") or ""),
                aliases=[str(a) for a in raw.get("aliases") or []],
                remotes=[str(r) for r in raw.get("remotes") or []],
                paths=[str(p) for p in raw.get("paths") or []],
                capture=bool(raw.get("capture", True)),
            )
        return result

    def save_projects(self, projects: dict[str, Project]) -> None:
        def strings(values: list[str]) -> str:
            return "[" + ", ".join(json.dumps(v, ensure_ascii=False) for v in values) + "]"

        parts = [REGISTRY_HEADER]
        for project in projects.values():
            parts.append(f"\n[projects.{json.dumps(project.slug, ensure_ascii=False)}]\n")
            parts.append(f"name = {json.dumps(project.name or project.slug, ensure_ascii=False)}\n")
            parts.append(f"aliases = {strings(project.aliases)}\n")
            parts.append(f"remotes = {strings(project.remotes)}\n")
            parts.append(f"paths = {strings(project.paths)}\n")
            if not project.capture:
                parts.append("capture = false\n")
        tmp = self.registry_path.with_suffix(".tmp")
        tmp.write_text("".join(parts))
        os.replace(tmp, self.registry_path)

    def add_project(self, project: Project) -> Project:
        with self.lock("registry"):
            projects = self.projects()
            base, n = project.slug, 2
            while project.slug in projects:
                project.slug = f"{base}-{n}"
                n += 1
            projects[project.slug] = project
            self.save_projects(projects)
        return project

    def update_project(self, slug: str, **changes: Any) -> Project:
        with self.lock("registry"):
            projects = self.projects()
            project = projects[slug]
            for key, value in changes.items():
                setattr(project, key, value)
            self.save_projects(projects)
        return project

    # git history ------------------------------------------------------------
    def is_git(self) -> bool:
        return (self.root / ".git").exists()

    def git(self, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            check=check,
            timeout=30,
        )

    def commit(self, message: str) -> bool:
        """Commit hub changes. Failures never break the calling command."""
        if not self.is_git():
            return False
        try:
            with self.lock("git"):
                self.git("add", "-A")
                if not self.git("diff", "--cached", "--quiet").returncode:
                    return False
                name, email = CURATOR_AUTHOR
                result = self.git(
                    "-c", f"user.name={name}", "-c", f"user.email={email}",
                    "commit", "-q", "--no-verify", "-m", message,
                )
                return result.returncode == 0
        except (OSError, subprocess.SubprocessError) as exc:
            self.log(f"commit failed: {exc}")
            return False
