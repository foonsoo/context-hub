import shutil

import pytest

from context_hub import knowledge, ops, review
from context_hub.hub import Project
from context_hub.index import Index
from context_hub.recall import recall
from context_hub.text import estimate_tokens


@pytest.fixture
def projects(hub, tmp_path):
    a, b = tmp_path / "proj-a", tmp_path / "proj-b"
    a.mkdir()
    b.mkdir()
    hub.add_project(Project("alpha", name="A 프로젝트", aliases=["a"], paths=[str(a)]))
    hub.add_project(Project("b-billing", name="B 프로젝트", aliases=["b", "결제 서비스"], paths=[str(b)]))
    return a, b


def test_note_creates_proposed_page_and_review_item(hub, projects):
    a, _ = projects
    result = ops.note(hub, "결정: 인증은 세션 쿠키 대신 JWT를 쓴다", cwd=str(a))
    assert result.ok and result.project == "alpha"
    page = knowledge.find(hub, result.page_id)
    assert page.status == "proposed" and page.type == "decision"
    assert page.sources == [result.ref]
    assert [i.kind for i in review.pending(hub)] == ["new"]
    assert hub.git("log", "--oneline").stdout.count("note:") == 1


def test_duplicate_note_adds_source_instead_of_new_page(hub, projects):
    a, _ = projects
    first = ops.note(hub, "배포는 make release 후 태그를 push한다", cwd=str(a), kind="procedure")
    second = ops.note(hub, "배포는 make release 후 태그를 push한다.", cwd=str(a), kind="procedure")
    assert second.page_id == first.page_id
    assert len(knowledge.find(hub, first.page_id).sources) == 2


def test_preference_goes_global_and_secrets_never_land(hub, projects):
    a, _ = projects
    result = ops.note(hub, "답변은 한국어로, 짧게 해 주는 걸 선호한다", cwd=str(a))
    assert result.project == "global"
    leaked = ops.note(hub, "스테이징 키는 sk-ant-api03-SECRETSECRETSECRET123 이다", cwd=str(a), kind="fact")
    assert "SECRETSECRET" not in knowledge.find(hub, leaked.page_id).path.read_text()
    assert all("SECRETSECRET" not in p.read_text() for p in hub.inbox.rglob("*.jsonl"))


def test_note_targets(hub, projects, isolated_env, make_repo):
    a, _ = projects
    assert ops.note(hub, "B 프로젝트 방식을 여기도 쓰기로 함", cwd=str(a)).project == "alpha"
    assert ops.note(hub, "b프로젝트 환불은 3일 내 처리", cwd=str(isolated_env)).project == "b-billing"
    loose = ops.note(hub, "아무 프로젝트도 아닌 메모", cwd=str(isolated_env))
    assert not loose.ok and "projects add" in loose.message, "no silent project or global fallback"
    assert ops.note(hub, "아무 프로젝트도 아닌 메모", cwd=str(isolated_env), project="global").project == "global"
    assert ops.note(hub, "x", project="없는거").ok is False
    repo = make_repo("newrepo", remote="git@github.com:me/newrepo.git")
    assert ops.note(hub, "새 저장소 첫 기록", cwd=str(repo / ".")).project == "newrepo"
    assert ops.note(hub, "   ").ok is False


def test_handoff_requires_a_project(hub, projects, isolated_env):
    a, _ = projects
    assert not ops.handoff(hub, cwd=str(isolated_env), next_steps=["x"]).ok
    assert not ops.handoff(hub, cwd=str(a)).ok
    result = ops.handoff(hub, cwd=str(isolated_env), project="a", next_steps=["로그인 테스트"])
    assert result.ok and result.project == "alpha"
    assert knowledge.load_now(hub, "alpha").sections["다음"] == ["로그인 테스트"]


def test_ab_scenario_from_the_design_doc(hub, projects):
    a, b = projects
    ops.handoff(hub, cwd=str(b), done=["결제 웹훅 재시도 로직"], next_steps=["환불 API 테스트 작성"],
                blockers=["PG사 샌드박스 키 발급 대기"])
    ops.note(hub, "TODO 정산 배치 시간대를 UTC로 통일", cwd=str(b))
    ops.note(hub, "결정: 결제 웹훅은 최대 5회 지수 백오프로 재시도", cwd=str(b))
    ops.handoff(hub, cwd=str(a), next_steps=["A의 로그인 화면 개선"])
    out = recall(hub, "b프로젝트 작업진행상태와 할일 알려줘", cwd=str(a)).text
    first = out.splitlines()[0]
    assert "B 프로젝트 (b-billing)" in first and "현재 폴더는 A 프로젝트" in first
    assert "환불 API 테스트 작성" in out and "PG사 샌드박스 키" in out
    assert "정산 배치 시간대를 UTC로 통일" in out
    assert "지수 백오프" in out, "status questions include recent records"
    assert "로그인 화면" not in out, "A's state must not leak into B's answer"


def test_recall_from_unrelated_folder_lists_projects(hub, projects, isolated_env):
    out = recall(hub, "뭐 하고 있었지", cwd=str(isolated_env)).text
    assert out.startswith("[프로젝트: 없음")
    assert "alpha" in out and "b-billing" in out


def test_ambiguous_and_unknown_projects_are_reported(hub, projects):
    hub.add_project(Project("b-admin", name="B 어드민", aliases=["결제 서비스"]))
    out = recall(hub, "결제 서비스 상태").text
    assert "여러 프로젝트" in out and "b-admin" in out and "b-billing" in out
    out = recall(hub, "상태", project="zzz").text
    assert "'zzz' 프로젝트를 찾지 못했습니다" in out


def test_all_projects_summary(hub, projects):
    a, b = projects
    ops.handoff(hub, cwd=str(a), next_steps=["A 다음"])
    ops.handoff(hub, cwd=str(b), next_steps=["B 다음"])
    out = recall(hub, "전체 할 일", project="*").text
    assert "A 다음" in out and "B 다음" in out


def test_korean_search_matches_inflected_words(hub, projects):
    a, _ = projects
    ops.note(hub, "결정: 결제 실패 시 사용자에게 재시도 버튼을 노출한다", cwd=str(a))
    ops.note(hub, "로그 보관 기간은 30일이다", cwd=str(a))
    out = recall(hub, "결제가 실패하면 어떻게 하기로 했지?", cwd=str(a)).text
    assert "재시도 버튼" in out and "보관 기간" not in out


def test_superseded_decision_is_hidden_but_referenced(hub, projects):
    a, _ = projects
    old = ops.note(hub, "결정: 데이터베이스는 SQLite를 쓴다", cwd=str(a))
    new = ops.note(hub, "결정: 데이터베이스는 PostgreSQL로 바꾼다", cwd=str(a))
    page = knowledge.find(hub, new.page_id)
    page.meta["links"] = {"supersedes": [old.page_id]}
    knowledge.save(hub, page)
    old_page = knowledge.find(hub, old.page_id)
    old_page.meta["status"] = "superseded"
    knowledge.save(hub, old_page)
    out = recall(hub, "데이터베이스 뭐 쓰기로 했지", cwd=str(a)).text
    assert "PostgreSQL" in out and "이전 결정: 데이터베이스는 SQLite를 쓴다" in out
    assert out.count("SQLite") == 1


def test_graph_neighbors_are_added(hub, projects):
    a, _ = projects
    concept = ops.note(hub, "인증 흐름: 로그인 → 토큰 발급 → 갱신", cwd=str(a), kind="concept")
    dep = ops.note(hub, "토큰 만료는 15분", cwd=str(a), kind="fact")
    page = knowledge.find(hub, dep.page_id)
    page.meta["links"] = {"about": [concept.page_id]}
    knowledge.save(hub, page)
    out = recall(hub, "토큰 만료 시간", cwd=str(a)).text
    assert "인증 흐름" in out and "연결: about" in out


@pytest.mark.parametrize("budget", [200, 500, 1500])
def test_budget_is_respected(hub, projects, budget):
    a, _ = projects
    for i in range(120):
        ops.note(hub, f"결제 모듈 사실 {i}: " + "결제 재시도 정책 세부 내용 " * 8, cwd=str(a), kind="fact")
    ops.handoff(hub, cwd=str(a), next_steps=[f"다음 작업 {i} 결제" for i in range(8)])
    out = recall(hub, "결제 재시도 정책", cwd=str(a), budget=budget).text
    assert estimate_tokens(out) <= budget + 40


def test_index_recovers_from_deletion_and_corruption(hub, projects):
    a, _ = projects
    ops.note(hub, "캐시 TTL은 60초", cwd=str(a))
    shutil.rmtree(hub.index_path.parent)
    assert "캐시 TTL" in recall(hub, "캐시 TTL", cwd=str(a)).text
    hub.index_path.write_bytes(b"not a database")
    for suffix in ("-wal", "-shm"):
        hub.index_path.with_name(hub.index_path.name + suffix).unlink(missing_ok=True)
    assert "캐시 TTL" in recall(hub, "캐시 TTL", cwd=str(a)).text


def test_hand_edited_files_are_picked_up(hub, projects):
    a, _ = projects
    result = ops.note(hub, "API 타임아웃은 30초", cwd=str(a))
    page = knowledge.find(hub, result.page_id)
    page.path.write_text(page.path.read_text().replace("30초", "45초"))
    with Index(hub) as index:
        assert "45초" in index.get(result.page_id).body
