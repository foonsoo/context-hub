import json
import os
import time

import pytest

from context_hub import curator, inbox, knowledge, ops, review
from context_hub.hub import Project


class FakeExtractor:
    def __init__(self, respond):
        self.respond = respond
        self.prompts = []

    def extract(self, prompt):
        self.prompts.append(prompt)
        return self.respond(prompt)


def _lines(prompt):
    return [int(line.split()[0][1:]) for line in prompt.splitlines() if line.startswith("L") and line[1:2].isdigit()]


def op(kind="create", type_="decision", title="제목", content="내용", start=None, end=None, target="", about=(), depends=()):
    return {"op": kind, "type": type_, "title": title, "content": content, "evidence_start": start,
            "evidence_end": end, "target_id": target, "about": list(about), "depends_on": list(depends)}


NO_NOW = {"has_update": False, "goal": "", "in_progress": [], "next": [], "blockers": [], "done": []}


@pytest.fixture
def svc(hub, tmp_path):
    path = tmp_path / "svc"
    path.mkdir()
    hub.add_project(Project("svc", name="Service", paths=[str(path)]))
    return path


def session(hub, svc, turns, name="claude-code-s1.jsonl", project="svc"):
    records = [{"client": "claude-code", "session": "s1", "cwd": str(svc), "project": project,
                "role": role, "text": text, "source": "transcript"} for role, text in turns]
    file = inbox.day_dir(hub) / name
    inbox.append(hub, file, records)
    return file


LONG = "이 세션에서 결제 재시도 정책을 논의했다. " * 25


def test_create_append_supersede_dispute_and_now(hub, svc):
    active = ops.note(hub, "결정: 재시도는 3회", cwd=str(svc))
    review.approve(hub, review.pending(hub)[0])
    proposed = ops.note(hub, "웹훅 서명은 HMAC-SHA256", cwd=str(svc), kind="fact")
    file = session(hub, svc, [("user", LONG), ("assistant", "재시도는 5회로 바꾸자"), ("user", "좋아, 5회로 하자")])

    def respond(prompt):
        lines = _lines(prompt)
        first, last = lines[0], lines[-1]
        assert active.page_id in prompt, "existing knowledge is offered for deduplication"
        return {
            "operations": [
                op("create", "procedure", "재시도 테스트 방법", "모의 PG로 5회 실패를 재현한다", first, last),
                op("append", "fact", "서명", "키는 매달 교체", first, first, target=proposed.page_id),
                op("supersede", "decision", "재시도는 5회", "부하를 고려해 5회로 늘렸다", first, last, target=active.page_id),
                op("dispute", "decision", "3회 설정과 충돌", "운영 설정은 7회", first, last, target=active.page_id),
                op("create", "fact", "근거 밖", "x", last + 5, last + 6),
                op("append", "fact", "없는 대상", "x", first, last, target="fact-nope"),
            ],
            "now": {**NO_NOW, "has_update": True, "next": ["5회 재시도 배포"], "done": ["재시도 정책 결정"]},
        }

    fake = FakeExtractor(respond)
    report = curator.curate(hub, fake, quiet_seconds=0)
    assert (report.sessions, report.calls, report.created, report.appended) == (1, 1, 1, 1)
    assert report.queued == 2 and report.rejected_ops == 2 and report.now_updates == 1
    kinds = sorted(i.kind for i in review.pending(hub))
    assert kinds == ["dispute", "new", "new", "supersede"]
    assert "키는 매달 교체" in knowledge.find(hub, proposed.page_id).body
    assert knowledge.find(hub, active.page_id).status == "active", "curator never changes trusted pages directly"
    new_pages = [p for p in knowledge.iter_pages(hub) if p.title == "재시도 테스트 방법"]
    assert new_pages[0].status == "proposed"
    assert new_pages[0].sources[0].startswith(file.relative_to(hub.root).as_posix() + "#L")
    assert knowledge.load_now(hub, "svc").sections["다음"] == ["5회 재시도 배포"]
    assert curator.pending(hub, quiet_seconds=0) == []
    assert curator.curate(hub, fake, quiet_seconds=0).calls == 0, "processed sessions are not re-sent"


def test_append_to_human_edited_page_goes_to_review(hub, svc):
    target = ops.note(hub, "캐시 TTL 60초", cwd=str(svc), kind="fact")
    page = knowledge.find(hub, target.page_id)
    page.path.write_text(page.path.read_text().replace("60초", "90초 (사람이 고침)"))
    session(hub, svc, [("user", LONG)])
    fake = FakeExtractor(lambda p: {"operations": [op("append", "fact", "t", "추가", _lines(p)[0], _lines(p)[0], target=target.page_id)], "now": NO_NOW})
    curator.curate(hub, fake, quiet_seconds=0)
    assert "추가" not in knowledge.find(hub, target.page_id).body
    assert any(i.kind == "append" for i in review.pending(hub))


def test_duplicate_create_becomes_append(hub, svc):
    existing = ops.note(hub, "배포 전에 마이그레이션을 먼저 실행한다", cwd=str(svc), kind="procedure")
    session(hub, svc, [("user", LONG)])
    fake = FakeExtractor(lambda p: {"operations": [op("create", "procedure", "배포 순서", "배포 전에 마이그레이션을 먼저 실행한다", _lines(p)[0], _lines(p)[0])], "now": NO_NOW})
    report = curator.curate(hub, fake, quiet_seconds=0)
    assert report.created == 0 and report.appended == 1
    assert len(knowledge.find(hub, existing.page_id).sources) == 2


def test_llm_output_is_redacted(hub, svc):
    session(hub, svc, [("user", LONG)])
    fake = FakeExtractor(lambda p: {"operations": [op("create", "fact", "키", "토큰은 ghp_abcdefghijklmnopqrstuvwxyz0123456789AB", _lines(p)[0], _lines(p)[0])], "now": NO_NOW})
    curator.curate(hub, fake, quiet_seconds=0)
    assert all("ghp_" not in p.path.read_text() for p in knowledge.iter_pages(hub))


def test_skips_trivial_unassigned_and_capture_off(hub, svc, tmp_path):
    session(hub, svc, [("user", "ㅇㅋ")], name="claude-code-tiny.jsonl")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    records = [{"role": "user", "text": LONG, "cwd": str(scratch), "project": None}]
    inbox.append(hub, inbox.day_dir(hub) / "codex-x.jsonl", records)
    hub.update_project("svc", capture=False)
    session(hub, svc, [("user", LONG)], name="claude-code-off.jsonl")
    fake = FakeExtractor(lambda p: pytest.fail("should not be called"))
    report = curator.curate(hub, fake, quiet_seconds=0)
    assert report.calls == 0 and curator.pending(hub, quiet_seconds=0) == []


def test_failure_keeps_session_pending_and_budget_limits(hub, svc, monkeypatch):
    session(hub, svc, [("user", LONG)])

    def boom(prompt):
        raise RuntimeError("network down")

    report = curator.curate(hub, FakeExtractor(boom), quiet_seconds=0)
    assert report.errors and len(curator.pending(hub, quiet_seconds=0)) == 1
    monkeypatch.setenv("CTX_CURATOR_DAILY_CALLS", "1")
    report = curator.curate(hub, FakeExtractor(lambda p: {"operations": [], "now": NO_NOW}), quiet_seconds=0)
    assert report.calls == 0 and "한도" in report.skipped[0]


def test_quiet_period_and_incremental_processing(hub, svc):
    file = session(hub, svc, [("user", LONG)])
    assert curator.pending(hub, quiet_seconds=600) == []
    old = time.time() - 3600
    os.utime(file, (old, old))
    assert len(curator.pending(hub, quiet_seconds=600)) == 1
    seen = []
    fake = FakeExtractor(lambda p: seen.append(_lines(p)) or {"operations": [], "now": NO_NOW})
    curator.curate(hub, fake, quiet_seconds=0)
    session(hub, svc, [("user", LONG + " 두 번째")])
    curator.curate(hub, fake, quiet_seconds=0)
    assert seen == [[1], [2]]


def test_large_sessions_are_chunked(hub, svc, monkeypatch):
    monkeypatch.setattr(curator, "CHUNK_CHARS", 2000)
    session(hub, svc, [("user", "가" * 900) for _ in range(6)])
    fake = FakeExtractor(lambda p: {"operations": [], "now": NO_NOW})
    report = curator.curate(hub, fake, quiet_seconds=0)
    assert report.calls == 3
    assert all(len(_lines(p)) == 2 for p in fake.prompts)


def test_unknown_project_session_registers_git_repo(hub, make_repo):
    repo = make_repo("fresh", remote="git@github.com:me/fresh.git")
    inbox.append(hub, inbox.day_dir(hub) / "claude-code-z.jsonl",
                 [{"role": "user", "text": LONG, "cwd": str(repo), "project": None}])
    fake = FakeExtractor(lambda p: {"operations": [op("create", "fact", "사실", "내용", _lines(p)[0], _lines(p)[0])], "now": NO_NOW})
    curator.curate(hub, fake, quiet_seconds=0)
    assert "fresh" in hub.projects()
    assert [p.project for p in knowledge.iter_pages(hub)] == ["fresh"]


def test_schema_is_strict_compatible():
    def walk(node):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
            for child in node["properties"].values():
                walk(child)
        if node.get("type") == "array":
            walk(node["items"])

    walk(curator.OPERATION_SCHEMA)
    json.dumps(curator.OPERATION_SCHEMA)


def test_without_extractor_nothing_breaks(hub, svc):
    session(hub, svc, [("user", LONG)])
    report = curator.curate(hub, None)
    assert report.skipped and len(curator.pending(hub, quiet_seconds=0)) == 1


def test_claude_cli_extractor_contract(hub, svc, tmp_path, monkeypatch):
    """A fake `claude` binary checks the flags that keep curation isolated and cheap."""
    import stat
    log = tmp_path / "argv.json"
    fake = tmp_path / "bin" / "claude"
    fake.parent.mkdir()
    fake.write_text(f"""#!{__import__('sys').executable}
import json, os, sys
argv = sys.argv[1:]
prompt = sys.stdin.read()
json.dump({{"argv": argv, "hook_disabled": os.environ.get("CTX_HOOK_DISABLE"), "has_lines": "L1 user:" in prompt}}, open({str(log)!r}, "w"))
out = {{"operations": [], "now": {{"has_update": True, "goal": "", "in_progress": [], "next": ["CLI로 정리됨"], "blockers": [], "done": []}}}}
print(json.dumps({{"is_error": False, "result": json.dumps(out), "structured_output": out}}))
""")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    monkeypatch.setenv("CTX_CURATOR", "claude")
    session(hub, svc, [("user", LONG)])
    extractor = curator.default_extractor()
    assert isinstance(extractor, curator.ClaudeCliExtractor)
    report = curator.curate(hub, extractor, quiet_seconds=0)
    assert report.now_updates == 1 and not report.errors
    seen = json.loads(log.read_text())
    assert seen["hook_disabled"] == "1" and seen["has_lines"]
    for flag in ("--json-schema", "--no-session-persistence", "--strict-mcp-config", "--setting-sources"):
        assert flag in seen["argv"]
    assert seen["argv"][seen["argv"].index("--tools") + 1] == ""
    assert knowledge.load_now(hub, "svc").sections["다음"] == ["CLI로 정리됨"]


def test_default_extractor_modes(monkeypatch):
    monkeypatch.setenv("CTX_CURATOR", "off")
    assert curator.default_extractor() is None
    monkeypatch.setenv("CTX_CURATOR", "claude")
    monkeypatch.setenv("PATH", "/nonexistent")
    assert curator.default_extractor() is None


def test_scratch_folder_session_goes_to_the_project_the_user_names(hub, svc, tmp_path):
    scratch = tmp_path / "notes-folder"
    scratch.mkdir()
    hub.update_project("svc", aliases=["결제 서비스"])
    turns = [("user", "결제 서비스 리뷰해줘 " + LONG), ("assistant", "네"), ("user", "결제 서비스 테스트도 봐줘"),
             ("user", "svc 배포는 다음 주")]
    records = [{"role": r, "text": t, "cwd": str(scratch), "project": None} for r, t in turns]
    inbox.append(hub, inbox.day_dir(hub) / "claude-code-n.jsonl", records)
    fake = FakeExtractor(lambda p: {"operations": [], "now": NO_NOW})
    assert curator.curate(hub, fake, quiet_seconds=0).sessions == 1
    assert "PROJECT: Service (svc)" in fake.prompts[0]
    assert "notes-folder" not in hub.projects()


def test_unassigned_sessions_are_retried_after_registry_changes(hub, svc, tmp_path):
    scratch = tmp_path / "loose"
    scratch.mkdir()
    records = [{"role": "user", "text": "주문 시스템 " + LONG, "cwd": str(scratch), "project": None},
               {"role": "user", "text": "주문 시스템 테스트", "cwd": str(scratch), "project": None}]
    inbox.append(hub, inbox.day_dir(hub) / "claude-code-u.jsonl", records)
    fake = FakeExtractor(lambda p: {"operations": [], "now": NO_NOW})
    report = curator.curate(hub, fake, quiet_seconds=0)
    assert report.sessions == 0 and "보류" in report.skipped[0]
    assert curator.pending(hub, quiet_seconds=0) == []
    time.sleep(0.05)
    hub.update_project("svc", aliases=["주문 시스템"])
    assert len(curator.pending(hub, quiet_seconds=0)) == 1
    assert curator.curate(hub, fake, quiet_seconds=0).sessions == 1
    assert curator.pending(hub, quiet_seconds=0) == []
