import json
import multiprocessing
import os

import pytest

from context_hub import inbox, knowledge
from context_hub.hub import Hub


def _writer(root, worker, count):
    os.environ["CTX_HOME"] = root
    hub = Hub(root)
    for i in range(count):
        inbox.add_note(hub, {"kind": "fact", "text": f"worker {worker} note {i}"})


def test_concurrent_appends_from_processes_keep_every_line(hub):
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_writer, args=(str(hub.root), w, 25)) for w in range(8)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
        assert p.exitcode == 0
    [file] = inbox.files(hub)
    lines = file.read_text().splitlines()
    assert len(lines) == 200
    texts = {json.loads(line)["text"] for line in lines}
    assert len(texts) == 200


def test_refs_point_at_the_written_line_and_redact(hub):
    refs = inbox.append(hub, hub.inbox / "x.jsonl", [
        {"text": "first"},
        {"text": "key sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUV"},
        {"text": "x" * 9000},
    ])
    assert refs == ["inbox/x.jsonl#L1", "inbox/x.jsonl#L2", "inbox/x.jsonl#L3"]
    assert inbox.resolve_ref(hub, refs[0])["text"] == "first"
    assert "sk-ant" not in inbox.resolve_ref(hub, refs[1])["text"]
    assert inbox.resolve_ref(hub, refs[2])["text"].endswith("…[truncated]")
    assert inbox.resolve_ref(hub, "inbox/x.jsonl#L2-L3")["text"].startswith("key")
    assert inbox.resolve_ref(hub, "inbox/missing.jsonl#L1") is None
    assert oct(os.stat(hub.inbox / "x.jsonl").st_mode & 0o777) == "0o600"


def test_page_roundtrip_and_human_edit_detection(hub):
    page = knowledge.new_page("decision", "DB를 PostgreSQL로", "동시 쓰기가 필요하다.", "demo", ["inbox/a#L1"])
    knowledge.save(hub, page)
    loaded = knowledge.load(page.path)
    assert loaded.meta == page.meta and loaded.body == page.body
    assert not loaded.human_edited()
    page.path.write_text(page.path.read_text().replace("필요하다", "필요하다. (사람이 수정)"))
    assert knowledge.load(page.path).human_edited()
    assert page.path.parent.name == "decisions"
    assert knowledge.find(hub, page.id).title == "DB를 PostgreSQL로"


def test_unreadable_page_is_skipped(hub):
    bad = hub.knowledge / "global" / "facts" / "broken.md"
    bad.parent.mkdir(parents=True)
    bad.write_text("---\nid: [unclosed\n---\n")
    good = knowledge.save(hub, knowledge.new_page("fact", "ok", "body", None, []))
    assert [p.id for p in knowledge.iter_pages(hub)] == [good.id]


@pytest.mark.parametrize("text,kind", [
    ("결정: 캐시는 Redis로 간다", "decision"),
    ("TODO 로그인 테스트 보강", "task"),
    ("나는 한국어 답변을 선호한다", "preference"),
    ("제약: 프로덕션 DB에 직접 쓰기 금지", "constraint"),
    ("배포 절차: make release 후 태그", "procedure"),
    ("API 응답 시간은 평균 120ms", "fact"),
])
def test_guess_type(text, kind):
    assert knowledge.guess_type(text) == kind


def test_title_strips_prefixes():
    assert knowledge.title_from("결정: 캐시는 Redis로 간다\n이유는...") == "캐시는 Redis로 간다"
    assert len(knowledge.title_from("가" * 200)) == 60


def test_handoff_semantics():
    doc = knowledge.Now(project="p", title="P")
    knowledge.apply_handoff(doc, next_steps=["환불 API 테스트", "정산 배치"], blockers=["키 대기"], goal="결제 안정화")
    assert doc.sections["다음"] == ["환불 API 테스트", "정산 배치"]
    knowledge.apply_handoff(doc, done=["환불 API 테스트 작성 완료"], next_steps=None)
    assert doc.sections["다음"] == ["정산 배치"], "completed items leave the next list"
    assert doc.sections["막힘"] == ["키 대기"], "omitted blockers are kept"
    assert doc.sections["최근 완료"][0].endswith("환불 API 테스트 작성 완료")
    knowledge.apply_handoff(doc, blockers=[])
    assert doc.sections["막힘"] == []
    assert doc.sections["목표"] == ["결제 안정화"]


def test_now_stays_under_limit_and_roundtrips(hub):
    doc = knowledge.Now(project="p", title="P")
    for i in range(40):
        knowledge.apply_handoff(doc, done=[f"작업 {i} " + "설명" * 30], next_steps=[f"다음 {i} " + "내용" * 40] * 8)
        knowledge.record_decision(doc, f"결정 {i}")
    assert len(doc.text()) <= knowledge.NOW_LIMIT
    knowledge.save_now(hub, doc)
    again = knowledge.load_now(hub, "p")
    assert again.sections == {k: v for k, v in doc.sections.items() if v}
