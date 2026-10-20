"""Decide which project a request is about.

Reads use: explicit ``project`` argument, then project names mentioned in the
question, then the working directory. Writes use: explicit argument, then the
working directory, and only fall back to names in the text when the directory
belongs to no project, so a note that merely mentions another project is not
filed under it.
"""

from __future__ import annotations

import functools
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .hub import Hub, Project, slugify

ALL = "*"


@dataclass
class GitInfo:
    root: str
    common_root: str
    remote: str | None


@dataclass
class Resolution:
    slugs: list[str] = field(default_factory=list)
    how: str = "none"
    cwd_slug: str | None = None
    ambiguous: list[list[str]] = field(default_factory=list)
    unknown: str | None = None

    @property
    def slug(self) -> str | None:
        return self.slugs[0] if len(self.slugs) == 1 else None

    @property
    def all_projects(self) -> bool:
        return self.how == "all"


def canonical_path(path: str | os.PathLike) -> str:
    """Resolve symlinks and restore on-disk letter case (macOS is case-insensitive)."""
    real = os.path.realpath(os.path.expanduser(os.fspath(path)))
    return _case_fixed(real)


@functools.lru_cache(maxsize=512)
def _case_fixed(real: str) -> str:
    parts = Path(real).parts
    if not parts:
        return real
    current = Path(parts[0])
    for part in parts[1:]:
        try:
            entries = os.listdir(current)
        except OSError:
            current = current / part
            continue
        if part not in entries:
            folded = [e for e in entries if e.casefold() == part.casefold()]
            if len(folded) == 1:
                part = folded[0]
        current = current / part
    return str(current)


def normalize_remote(url: str) -> str:
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    match = re.match(r"^[a-z][a-z0-9+.-]*://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)$", url, re.I)
    if match:
        host, path = match.groups()
    else:
        match = re.match(r"^(?:[^@/]+@)?([^/:]+):(.+)$", url)
        if match and not re.match(r"^[a-z]:\\", url, re.I):
            host, path = match.groups()
        else:
            return "file:" + canonical_path(url)
    return f"{host.lower()}/{path.strip('/').lower()}"


def git_info(cwd: str) -> GitInfo | None:
    def run(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", cwd, *args], capture_output=True, text=True, timeout=5
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    out = run("rev-parse", "--show-toplevel", "--git-common-dir")
    if not out:
        return None
    lines = out.splitlines()
    if len(lines) < 2:
        return None
    root = canonical_path(lines[0])
    common = lines[1]
    common_path = Path(common) if os.path.isabs(common) else Path(root) / common
    common_root = canonical_path(common_path.parent if common_path.name == ".git" else common_path)
    remote = run("config", "--get", "remote.origin.url")
    if not remote:
        names = (run("remote") or "").split()
        remote = run("config", "--get", f"remote.{names[0]}.url") if names else None
    return GitInfo(root=root, common_root=common_root, remote=normalize_remote(remote) if remote else None)


def repo_info(cwd: str) -> GitInfo | None:
    """git_info, ignoring repositories rooted at home or / (e.g. dotfiles)."""
    if not os.path.isdir(cwd):
        return None
    info = git_info(cwd)
    if info and _never_a_project(info.common_root):
        return None
    return info


def _is_within(path: str, ancestor: str) -> bool:
    return path == ancestor or path.startswith(ancestor.rstrip(os.sep) + os.sep)


def _never_a_project(path: str) -> bool:
    home = canonical_path(Path.home())
    return path in (home, os.sep) or len(Path(path).parts) <= 2


def project_for_cwd(hub: Hub, cwd: str | None, projects: dict[str, Project] | None = None) -> tuple[str | None, str]:
    if not cwd:
        return None, "none"
    projects = projects if projects is not None else hub.projects()
    path = canonical_path(cwd)
    best: tuple[int, str] | None = None
    for project in projects.values():
        for registered in project.paths:
            registered = canonical_path(registered)
            if _is_within(path, registered) and (best is None or len(registered) > best[0]):
                best = (len(registered), project.slug)
    if best:
        return best[1], "path"
    info = repo_info(path)
    if info and info.remote:
        for project in projects.values():
            if info.remote in project.remotes:
                return project.slug, "remote"
    if info:
        for project in projects.values():
            if any(canonical_path(p) in (info.root, info.common_root) for p in project.paths):
                return project.slug, "git-root"
    return None, "none"


_LATIN = re.compile(r"[0-9a-z]")
_PROJECT_WORD = re.compile(r"\s*(프로젝트|project|repo|레포)")


def _occurrences(text: str, label: str) -> list[tuple[int, int]]:
    spans, start = [], 0
    while True:
        index = text.find(label, start)
        if index < 0:
            return spans
        end = index + len(label)
        before = text[index - 1] if index else ""
        after = text[end] if end < len(text) else ""
        latin_edges = (_LATIN.match(label[0]) and before and _LATIN.match(before)) or (
            _LATIN.match(label[-1]) and after and _LATIN.match(after)
        )
        short_ok = len(label) >= 2 or _PROJECT_WORD.match(text, end)
        if not latin_edges and short_ok:
            spans.append((index, end))
        start = index + 1


def mentioned_projects(text: str, projects: dict[str, Project]) -> tuple[list[str], list[list[str]]]:
    """Return (mentioned slugs, ambiguous groups) for project names inside ``text``."""
    folded = re.sub(r"\s+", " ", text.casefold())
    variants = [folded, folded.replace(" ", "")]
    matches: list[tuple[int, int, int, str]] = []
    for variant_index, variant in enumerate(variants):
        for project in projects.values():
            for label in project.labels():
                label = re.sub(r"\s+", " ", label.casefold()).strip()
                if variant_index == 1:
                    label = label.replace(" ", "")
                if not label:
                    continue
                for start, end in _occurrences(variant, label):
                    matches.append((variant_index, start, end, project.slug))
    kept = [
        m for m in matches
        if not any(
            o[0] == m[0] and o[3] != m[3] and o[1] <= m[1] and m[2] <= o[2] and (o[2] - o[1]) > (m[2] - m[1])
            for o in matches
        )
    ]
    by_span: dict[tuple[int, int, int], set[str]] = {}
    for variant_index, start, end, slug in kept:
        by_span.setdefault((variant_index, start, end), set()).add(slug)
    ambiguous = sorted({tuple(sorted(s)) for s in by_span.values() if len(s) > 1})
    ambiguous_slugs = {slug for group in ambiguous for slug in group}
    slugs: list[str] = []
    for (_, start, _), group in sorted(by_span.items(), key=lambda item: item[0][1]):
        for slug in sorted(group):
            if slug not in ambiguous_slugs and slug not in slugs:
                slugs.append(slug)
    return slugs, [list(group) for group in ambiguous]


def _match_label(value: str, projects: dict[str, Project]) -> list[str]:
    wanted = re.sub(r"\s+", "", value.casefold())
    return [
        project.slug for project in projects.values()
        if any(re.sub(r"\s+", "", label.casefold()) == wanted for label in project.labels())
    ]


def resolve(hub: Hub, cwd: str | None = None, text: str | None = None,
            project: str | None = None, for_write: bool = False) -> Resolution:
    projects = hub.projects()
    cwd_slug, cwd_how = project_for_cwd(hub, cwd, projects)
    if project:
        if project.strip() == ALL:
            if for_write:
                return Resolution(how="explicit", cwd_slug=cwd_slug, unknown=project)
            return Resolution(slugs=sorted(projects), how="all", cwd_slug=cwd_slug)
        if project.strip().casefold() == "global":
            return Resolution(slugs=["global"], how="explicit", cwd_slug=cwd_slug)
        found = _match_label(project, projects)
        if len(found) == 1:
            return Resolution(slugs=found, how="explicit", cwd_slug=cwd_slug)
        if len(found) > 1:
            return Resolution(how="explicit", cwd_slug=cwd_slug, ambiguous=[found])
        return Resolution(how="explicit", cwd_slug=cwd_slug, unknown=project)

    mentioned, ambiguous = mentioned_projects(text or "", projects) if text else ([], [])
    if for_write:
        if cwd_slug:
            return Resolution(slugs=[cwd_slug], how=cwd_how, cwd_slug=cwd_slug)
        if ambiguous:
            return Resolution(how="text", ambiguous=ambiguous)
        if len(mentioned) == 1:
            return Resolution(slugs=mentioned, how="text")
        if len(mentioned) > 1:
            return Resolution(how="text", ambiguous=[mentioned])
        return Resolution(how="none")
    if mentioned:
        return Resolution(slugs=mentioned, how="text", cwd_slug=cwd_slug, ambiguous=ambiguous)
    if ambiguous:
        return Resolution(how="text", cwd_slug=cwd_slug, ambiguous=ambiguous)
    if cwd_slug:
        return Resolution(slugs=[cwd_slug], how=cwd_how, cwd_slug=cwd_slug)
    return Resolution(how="none")


def register_cwd(hub: Hub, cwd: str) -> str | None:
    """Create a project for ``cwd`` (used on the first write). Home and / never qualify."""
    slug, _ = project_for_cwd(hub, cwd)
    if slug:
        return slug
    path = canonical_path(cwd)
    if not os.path.isdir(path):
        return None
    info = repo_info(path)
    if info:
        base = info.remote.rsplit("/", 1)[-1] if info.remote and not info.remote.startswith("file:") else Path(info.common_root).name
        project = Project(
            slug=slugify(base), name=base,
            remotes=[info.remote] if info.remote else [],
            paths=[info.common_root],
        )
    else:
        if _never_a_project(path):
            return None
        name = Path(path).name
        project = Project(slug=slugify(name), name=name, paths=[path])
    return hub.add_project(project).slug
