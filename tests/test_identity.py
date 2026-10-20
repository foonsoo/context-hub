import os
import sys

import pytest

from context_hub.hub import Project
from context_hub.identity import (
    canonical_path, mentioned_projects, normalize_remote, project_for_cwd, register_cwd, resolve,
)

from conftest import git


@pytest.mark.parametrize("url,expected", [
    ("git@github.com:Me/Billing.git", "github.com/me/billing"),
    ("https://github.com/me/billing", "github.com/me/billing"),
    ("https://github.com/me/billing.git/", "github.com/me/billing"),
    ("ssh://git@github.com:22/me/billing.git", "github.com/me/billing"),
    ("https://user:pw@gitlab.example.com/group/sub/repo.git", "gitlab.example.com/group/sub/repo"),
    ("git@gitlab.example.com:group/sub/repo", "gitlab.example.com/group/sub/repo"),
])
def test_normalize_remote(url, expected):
    assert normalize_remote(url) == expected


def test_local_path_remote(tmp_path):
    assert normalize_remote(str(tmp_path)) == "file:" + canonical_path(tmp_path)


def test_subdirectory_worktree_and_second_clone_share_a_project(hub, make_repo, tmp_path):
    repo = make_repo("billing", remote="git@github.com:me/billing.git")
    slug = register_cwd(hub, str(repo))
    (repo / "src" / "deep").mkdir(parents=True)
    assert project_for_cwd(hub, str(repo / "src" / "deep"))[0] == slug
    worktree = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", str(worktree), "-b", "feature")
    assert project_for_cwd(hub, str(worktree))[0] == slug
    clone = tmp_path / "elsewhere" / "billing-copy"
    clone.parent.mkdir()
    git(tmp_path, "clone", "-q", str(repo), str(clone))
    git(clone, "remote", "set-url", "origin", "https://github.com/me/billing")
    assert project_for_cwd(hub, str(clone)) == (slug, "remote")


def test_remoteless_worktree_maps_to_main_repo(hub, make_repo, tmp_path):
    repo = make_repo("local-only")
    slug = register_cwd(hub, str(repo))
    worktree = tmp_path / "wt2"
    git(repo, "worktree", "add", "-q", str(worktree), "-b", "x")
    assert project_for_cwd(hub, str(worktree))[0] == slug


@pytest.mark.skipif(sys.platform != "darwin", reason="case-insensitive filesystem")
def test_letter_case_does_not_split_projects(hub, tmp_path):
    folder = tmp_path / "Repo" / "Claude"
    folder.mkdir(parents=True)
    slug = register_cwd(hub, str(folder))
    other = str(tmp_path / "repo" / "claude")
    assert canonical_path(other) == canonical_path(folder)
    assert project_for_cwd(hub, other)[0] == slug


def test_home_and_root_are_never_projects(hub, isolated_env):
    assert register_cwd(hub, str(isolated_env)) is None
    assert register_cwd(hub, "/") is None
    assert hub.projects() == {}


def test_dotfiles_repo_at_home_is_not_a_project(hub, isolated_env):
    git(isolated_env, "init", "-q")
    (isolated_env / "sub").mkdir()
    assert register_cwd(hub, str(isolated_env)) is None


def test_nested_paths_pick_longest_registration(hub, tmp_path):
    mono = tmp_path / "mono"
    (mono / "services" / "pay").mkdir(parents=True)
    hub.add_project(Project("mono", paths=[str(mono)]))
    hub.add_project(Project("pay", paths=[str(mono / "services" / "pay")]))
    assert project_for_cwd(hub, str(mono / "services" / "pay"))[0] == "pay"
    assert project_for_cwd(hub, str(mono / "services"))[0] == "mono"


@pytest.fixture
def ab(hub, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    hub.add_project(Project("alpha", name="A 프로젝트", aliases=["a", "알파"], paths=[str(a)]))
    hub.add_project(Project("b-billing", name="B 프로젝트", aliases=["b", "결제 서비스", "billing"], paths=[str(b)]))
    hub.add_project(Project("b-admin", name="B 어드민", aliases=["b-admin"]))
    return a, b


@pytest.mark.parametrize("text,expected", [
    ("b프로젝트 진행상태와 할일을 알려줘", ["b-billing"]),
    ("B 프로젝트 상태", ["b-billing"]),
    ("결제서비스 어디까지 했지", ["b-billing"]),
    ("billing repo 다음 작업", ["b-billing"]),
    ("b-admin 배포 상태", ["b-admin"]),
    ("a프로젝트와 b프로젝트 비교", ["alpha", "b-billing"]),
    ("add a test for the parser", []),
    ("abc billingual", []),
    ("알파 버전 출시", ["alpha"]),
])
def test_mentions(ab, hub, text, expected):
    slugs, ambiguous = mentioned_projects(text, hub.projects())
    assert slugs == expected
    assert ambiguous == []


def test_same_alias_on_two_projects_is_ambiguous(ab, hub):
    hub.update_project("b-admin", aliases=["b-admin", "결제 서비스"])
    slugs, ambiguous = mentioned_projects("결제 서비스 상태", hub.projects())
    assert slugs == []
    assert ambiguous == [["b-admin", "b-billing"]]


def test_read_prefers_named_project_over_cwd(ab, hub):
    a, _ = ab
    res = resolve(hub, cwd=str(a), text="B 프로젝트 진행 상태")
    assert res.slugs == ["b-billing"] and res.cwd_slug == "alpha" and res.how == "text"


def test_read_falls_back_to_cwd(ab, hub):
    a, _ = ab
    res = resolve(hub, cwd=str(a), text="테스트 실패 원인 찾아줘")
    assert res.slugs == ["alpha"] and res.how == "path"


def test_write_prefers_cwd_over_text(ab, hub):
    a, _ = ab
    res = resolve(hub, cwd=str(a), text="B 프로젝트 방식을 여기도 적용하기로 함", for_write=True)
    assert res.slug == "alpha"


def test_write_uses_text_when_cwd_unknown(ab, hub, tmp_path):
    res = resolve(hub, cwd=str(tmp_path), text="b프로젝트: 환불은 3일 내 처리", for_write=True)
    assert res.slug == "b-billing"
    res = resolve(hub, cwd=str(tmp_path), text="a프로젝트와 b프로젝트 공통 규칙", for_write=True)
    assert res.slug is None and res.ambiguous


def test_explicit_project(ab, hub):
    assert resolve(hub, project="결제 서비스").slugs == ["b-billing"]
    assert resolve(hub, project="nope").unknown == "nope"
    res = resolve(hub, project="*")
    assert res.all_projects and set(res.slugs) == {"alpha", "b-billing", "b-admin"}
    assert resolve(hub, project="*", for_write=True).unknown == "*"


def test_unregistered_git_repo_registers_by_remote_name(hub, make_repo):
    repo = make_repo("checkout-dir", remote="https://github.com/me/Real-Name.git")
    slug = register_cwd(hub, str(repo))
    project = hub.projects()[slug]
    assert slug == "real-name" and project.remotes == ["github.com/me/real-name"]


def test_slug_collisions_get_suffix(hub, make_repo, tmp_path):
    one = make_repo("api", remote="git@github.com:org1/api.git")
    two = make_repo("api", remote="git@github.com:org2/api.git", parent=tmp_path / "other")
    assert register_cwd(hub, str(one)) == "api"
    assert register_cwd(hub, str(two)) == "api-2"
    assert register_cwd(hub, str(two)) == "api-2"
