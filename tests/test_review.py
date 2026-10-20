import pytest

from context_hub import knowledge, ops, review
from context_hub.hub import Project


@pytest.fixture
def repo_dir(hub, tmp_path):
    path = tmp_path / "svc"
    path.mkdir()
    hub.add_project(Project("svc", name="Service", paths=[str(path)]))
    return path


def test_approve_new_decision_updates_now(hub, repo_dir):
    result = ops.note(hub, "결정: 큐는 SQS를 쓴다", cwd=str(repo_dir))
    [item] = review.pending(hub)
    assert "큐는 SQS를 쓴다" in review.describe(hub, item)
    review.approve(hub, item)
    page = knowledge.find(hub, result.page_id)
    assert page.status == "active" and page.meta["reviewed_by"] == "user"
    assert knowledge.load_now(hub, "svc").sections["최근 결정"] == ["큐는 SQS를 쓴다"]
    assert review.pending(hub) == []


def test_reject_archives(hub, repo_dir):
    result = ops.note(hub, "잘못된 추측", cwd=str(repo_dir), kind="fact")
    review.reject(hub, review.pending(hub)[0])
    page = knowledge.find(hub, result.page_id)
    assert page.status == "archived" and page.meta["rejected"] is True


def test_supersede_flow(hub, repo_dir):
    old = ops.note(hub, "결정: 로그는 파일에 남긴다", cwd=str(repo_dir))
    review.approve(hub, review.pending(hub)[0])
    new = knowledge.new_page("decision", "로그는 OpenTelemetry로 보낸다", "중앙 수집이 필요하다", "svc", ["inbox/x#L1"])
    knowledge.save(hub, new)
    item = review.add(hub, "supersede", page_id=new.id, target_id=old.page_id, project="svc")
    review.approve(hub, item)
    assert knowledge.find(hub, old.page_id).status == "superseded"
    assert knowledge.find(hub, old.page_id).meta["superseded_by"] == new.id
    replacement = knowledge.find(hub, new.id)
    assert replacement.status == "active" and replacement.links["supersedes"] == [old.page_id]


def test_append_dispute_and_alias(hub, repo_dir):
    target = ops.note(hub, "배포는 금요일에 하지 않는다", cwd=str(repo_dir), kind="constraint")
    review.approve(hub, review.pending(hub)[0])
    review.approve(hub, review.add(hub, "append", target_id=target.page_id, project="svc",
                                   content="예외: 보안 패치는 허용", sources=["inbox/y#L2"]))
    page = knowledge.find(hub, target.page_id)
    assert "보안 패치는 허용" in page.body and "inbox/y#L2" in page.sources
    review.approve(hub, review.add(hub, "dispute", target_id=target.page_id, project="svc",
                                   content="지난주 금요일 배포 기록 있음"))
    assert knowledge.find(hub, target.page_id).status == "disputed"
    review.approve(hub, review.add(hub, "alias", project="svc", extra={"alias": "서비스"}))
    assert "서비스" in hub.projects()["svc"].aliases


def test_missing_target_closes_item(hub, repo_dir):
    item = review.add(hub, "append", target_id="nope", project="svc", content="x")
    assert "닫았습니다" in review.approve(hub, item)
    assert review.pending(hub) == []


def test_human_edit_survives_approval(hub, repo_dir):
    result = ops.note(hub, "캐시는 5분", cwd=str(repo_dir), kind="fact")
    page = knowledge.find(hub, result.page_id)
    page.path.write_text(page.path.read_text().replace("5분", "10분 (확인함)"))
    review.approve(hub, review.pending(hub)[0])
    page = knowledge.find(hub, result.page_id)
    assert "10분 (확인함)" in page.body and page.status == "active"
    assert page.human_edited(), "approval must not reset the human-edit marker"


def test_superseded_and_forgotten_decisions_leave_now(hub, repo_dir):
    old = ops.note(hub, "결정: 로그는 파일에 남긴다", cwd=str(repo_dir))
    review.approve(hub, review.pending(hub)[0])
    assert knowledge.load_now(hub, "svc").sections["최근 결정"] == ["로그는 파일에 남긴다"]
    new = knowledge.save(hub, knowledge.new_page("decision", "로그는 OTel로 보낸다", "중앙 수집", "svc", []))
    review.approve(hub, review.add(hub, "supersede", page_id=new.id, target_id=old.page_id, project="svc"))
    assert knowledge.load_now(hub, "svc").sections["최근 결정"] == ["로그는 OTel로 보낸다"]
    review.forget_in_now(hub, knowledge.find(hub, new.id))
    assert not knowledge.load_now(hub, "svc").sections.get("최근 결정")
