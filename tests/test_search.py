"""Hybrid (lexical + semantic) search, with a deterministic fake embedder."""

import os

import pytest

from context_hub import embeddings, hooks, knowledge, ops
from context_hub.hub import Project
from context_hub.index import Index
from context_hub.recall import recall

CONCEPTS = [
    ("payment", "결제", "toss", "토스", "provider"),
    ("excel", "엑셀", "spreadsheet", "스프레드시트"),
    ("upload", "업로드", "올리"),
    ("backup", "백업"),
    ("docker", "도커", "container", "컨테이너"),
    ("image", "이미지"),
]


class FakeEmbedder:
    model = "fake-concepts-v1"

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += len(texts)
        out = []
        for text in texts:
            folded = text.casefold()
            vec = [0.2] + [1.0 if any(w in folded for w in group) else 0.0 for group in CONCEPTS]
            norm = sum(x * x for x in vec) ** 0.5
            out.append([x / norm for x in vec])
        return out


@pytest.fixture
def fake(monkeypatch):
    embedder = FakeEmbedder()
    monkeypatch.setattr(embeddings, "load", lambda hub: embedder)
    return embedder


@pytest.fixture
def svc(hub, tmp_path):
    path = tmp_path / "svc"
    path.mkdir()
    hub.add_project(Project("svc", name="Service", paths=[str(path)]))
    return path


def test_cross_language_paraphrase_needs_embeddings(hub, svc, monkeypatch):
    ops.note(hub, "결정: 결제는 토스로 간다", cwd=str(svc), confirmed=True)
    ops.note(hub, "로그 보관 기간은 30일", cwd=str(svc), confirmed=True)
    query = "which payment provider did we choose"
    lexical = recall(hub, query, cwd=str(svc), include_now=False)
    assert "토스" not in lexical.text and not lexical.relevant, "lexical search alone cannot bridge languages"
    embedder = FakeEmbedder()
    monkeypatch.setattr(embeddings, "load", lambda hub: embedder)
    result = recall(hub, query, cwd=str(svc), include_now=False)
    assert "토스로 간다" in result.text and "보관 기간" not in result.text
    assert result.relevant


def test_paraphrase_ranks_first(hub, svc, fake):
    target = ops.note(hub, "MailToDB는 Excel 업로드 중심으로 웹 기능을 확장", cwd=str(svc), kind="decision", confirmed=True)
    for i in range(10):
        ops.note(hub, f"웹 기능 메모 {i}: 화면 레이아웃 정리", cwd=str(svc), kind="fact", confirmed=True)
    with Index(hub, fake) as index:
        hits = index.search("엑셀로 올리는 방향으로 가기로 한 이유", ["svc"])
    assert hits[0][1].id == target.page_id


def test_relevance_gate_ignores_lexical_noise(hub, svc, fake):
    ops.note(hub, "Phase7 변경: 샘플 앱 화면 교체, Actions 보류", cwd=str(svc), confirmed=True)
    query = "Phase7 Actions 보류 도커 컨테이너"
    with Index(hub) as lexical_only:
        hits = lexical_only.search(query, ["svc"])
        assert lexical_only.relevant(hits), "lexical overlap alone looks relevant"
    with Index(hub, fake) as index:
        hits = index.search(query, ["svc"])
        assert hits, "the lexical candidate is still listed"
        assert not index.relevant(hits), "but without semantic support it is not injected unprompted"


def test_vectors_are_cached_and_refreshed_on_edit(hub, svc, fake):
    result = ops.note(hub, "백업은 매일 새벽 3시", cwd=str(svc), confirmed=True)
    with Index(hub, fake) as index:
        index.search("백업", ["svc"])
        first = fake.calls
        index.search("백업 주기", ["svc"])
        assert fake.calls == first + 1, "only the query is embedded the second time"
    page = knowledge.find(hub, result.page_id)
    page.path.write_text(page.path.read_text().replace("3시", "4시"))
    with Index(hub, fake) as index:
        before = fake.calls
        index.search("백업", ["svc"])
        assert fake.calls == before + 2, "the edited page is re-embedded"
        assert index.conn.execute("SELECT count(*) FROM vectors").fetchone()[0] == 1


def test_backfill_is_bounded_per_search(hub, svc, fake, monkeypatch):
    from context_hub import index as index_module
    monkeypatch.setattr(index_module, "VECTOR_BATCH_PER_SEARCH", 5)
    for i in range(12):
        knowledge.save(hub, knowledge.new_page("fact", f"사실 {i}", f"결제 관련 사실 {i}", "svc", []))
    with Index(hub, fake) as index:
        index.search("결제", ["svc"])
        assert index.conn.execute("SELECT count(*) FROM vectors").fetchone()[0] == 5
        done, left = index.sync_vectors()
        assert (done, left) == (7, 0)


def test_broken_embedder_falls_back_to_lexical(hub, svc, monkeypatch):
    class Broken:
        model = "broken"

        def embed(self, texts):
            raise RuntimeError("model file missing")

    monkeypatch.setattr(embeddings, "load", lambda hub: Broken())
    ops.note(hub, "캐시 TTL은 60초", cwd=str(svc), confirmed=True)
    assert "60초" in recall(hub, "캐시 TTL", cwd=str(svc)).text
    assert "semantic search failed" in (hub.state_dir / "ctx.log").read_text()


def test_prompt_hook_uses_semantic_gate(hub, svc, fake, capsys):
    import io, json
    ops.note(hub, "결정: 결제는 토스로 간다", cwd=str(svc), confirmed=True)
    hooks.run("prompt", hub, io.StringIO(json.dumps({"cwd": str(svc), "prompt": "which payment provider again?"})))
    out = capsys.readouterr().out
    assert "토스로 간다" in json.loads(out)["hookSpecificOutput"]["additionalContext"]
    hooks.run("prompt", hub, io.StringIO(json.dumps({"cwd": str(svc), "prompt": "도커 이미지 줄여줘"})))
    assert capsys.readouterr().out == ""


def test_config_command_enables_and_backfills(hub, svc, monkeypatch, capsys):
    from context_hub import cli
    for text in ("캐시 TTL은 60초", "배포는 화요일", "로그는 30일 보관"):
        ops.note(hub, text, cwd=str(svc), kind="fact", project="svc", confirmed=True)
    monkeypatch.setattr(embeddings, "installed", lambda: False)
    assert cli.main(["config", "embeddings", "on"]) == 1
    assert "pip install 'context-hub[embed]'" in capsys.readouterr().out
    monkeypatch.setattr(embeddings, "installed", lambda: True)
    monkeypatch.setattr(embeddings, "FastEmbedder", lambda model: FakeEmbedder())
    embeddings._CACHE.clear()
    assert cli.main(["config", "embeddings", "on"]) == 0
    out = capsys.readouterr().out
    assert "지식 벡터 3/3" in out and "의미 검색을 켰습니다" in out
    cli.main(["status"])
    assert "의미 검색: 켜짐 · 벡터 3/3" in capsys.readouterr().out
    cli.main(["config", "embeddings", "off"])
    assert embeddings.load(hub) is None
    embeddings._CACHE.clear()


@pytest.mark.slow
@pytest.mark.skipif(not embeddings.installed() or not os.environ.get("CTX_MODEL_CACHE"),
                    reason="needs fastembed and CTX_MODEL_CACHE with the model")
def test_real_model_bridges_korean_and_english(hub, svc, monkeypatch):
    monkeypatch.setenv("CTX_EMBEDDINGS", "on")
    embeddings._CACHE.clear()
    ops.note(hub, "MailToDB 통합 저장소는 ~/go/src/github.com/foonsoo/mail-to-db에 있다", cwd=str(svc), kind="fact", confirmed=True)
    ops.note(hub, "제품 우선순위: 개인 사용자 경험을 팀 기능보다 먼저 한다", cwd=str(svc), kind="decision", confirmed=True)
    ops.note(hub, "로그 보관 기간은 30일", cwd=str(svc), kind="fact", confirmed=True)
    out = recall(hub, "Where is the MailToDB repository located?", cwd=str(svc)).text
    assert out.index("mail-to-db") < out.index("30일") if "30일" in out else "mail-to-db" in out
    result = recall(hub, "개인 사용자를 먼저 챙기기로 했나?", cwd=str(svc), include_now=False)
    assert "개인 사용자 경험" in result.text and result.relevant
    assert not recall(hub, "오늘 점심 뭐 먹지", cwd=str(svc), include_now=False).relevant
    embeddings._CACHE.clear()
