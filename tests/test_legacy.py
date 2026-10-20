import sqlite3

import pytest

from context_hub import inbox, knowledge, legacy, review
from context_hub.hub import Project

SCHEMA = """
CREATE TABLE projects(id TEXT PRIMARY KEY, slug TEXT, name TEXT, description TEXT, created_at TEXT);
CREATE TABLE scopes(id TEXT PRIMARY KEY, project_id TEXT, name TEXT, path TEXT, created_at TEXT);
CREATE TABLE memories(id TEXT PRIMARY KEY, project_id TEXT, scope_id TEXT, type TEXT, status TEXT, title TEXT,
  content TEXT, confidence REAL, importance REAL, valid_from TEXT, valid_until TEXT, tags_json TEXT,
  created_at TEXT, updated_at TEXT, observed_at TEXT, last_confirmed_at TEXT, visibility TEXT);
CREATE TABLE events(id TEXT PRIMARY KEY, project_id TEXT, scope_id TEXT, session_id TEXT, kind TEXT, content TEXT,
  source_uri TEXT, metadata_json TEXT, content_hash TEXT, created_at TEXT, event_seq INTEGER);
CREATE TABLE memory_sources(memory_id TEXT, event_id TEXT, note TEXT, created_at TEXT);
"""


@pytest.fixture
def legacy_db(tmp_path, isolated_env, make_repo):
    repo = make_repo("context-memory", remote="git@github.com:foonsoo/context-memory.git")
    copy = tmp_path / "copies" / "context-memory-old"
    copy.parent.mkdir()
    import subprocess
    subprocess.run(["git", "clone", "-q", str(repo), str(copy)], check=True)
    subprocess.run(["git", "-C", str(copy), "remote", "set-url", "origin", "https://github.com/foonsoo/context-memory"], check=True)
    gone = tmp_path / "deleted-dir"
    workspace_copy = tmp_path / "chatgpt" / "context-memory"
    workspace_copy.mkdir(parents=True)
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    rows = [("p1", "context-memory", str(repo)), ("p2", "context-memory-b0cc", str(copy)),
            ("p3", "ym", str(isolated_env)), ("p4", "old-thing", str(gone)), ("p5", "workspace", "/"),
            ("p6", "context-memory-5504612a", str(workspace_copy)), ("p7", "empty-junk", str(tmp_path))]
    for pid, slug, path in rows:
        conn.execute("INSERT INTO projects VALUES (?,?,?,?,?)", (pid, slug, slug, "", f"2026-08-0{pid[1]}"))
        conn.execute("INSERT INTO scopes VALUES (?,?,?,?,?)", ("s" + pid, pid, "ws", path, ""))
    memories = [
        ("m1", "p1", "decision", "active", "DB는 SQLite", "로컬 단일 사용자라 SQLite"),
        ("m2", "p2", "summary", "active", "요약", "복사본에서 한 작업 요약"),
        ("m3", "p3", "preference", "active", "한국어 선호", "답변은 한국어로"),
        ("m4", "p4", "task", "proposed", "옛 할 일", "정리 필요"),
        ("m5", "p1", "fact", "superseded", "옛 사실", "버려짐"),
        ("m6", "p5", "fact", "rejected", "기각", "x"),
        ("m7", "p6", "decision", "active", "사본에서 내린 결정", "ChatGPT 작업 폴더에서 결정"),
    ]
    for mid, pid, kind, status, title, content in memories:
        conn.execute("INSERT INTO memories (id, project_id, type, status, title, content, created_at) VALUES (?,?,?,?,?,?,?)",
                     (mid, pid, kind, status, title, content, "2026-08-20T00:00:00"))
    conn.execute("INSERT INTO events VALUES ('e1','p1',NULL,NULL,'decision','SQLite로 결정함 (근거)',NULL,NULL,NULL,'2026-08-19',1)")
    conn.execute("INSERT INTO memory_sources VALUES ('m1','e1',NULL,NULL)")
    conn.commit()
    conn.close()
    return db


def test_plan_merges_clones_and_drops_junk_projects(hub, legacy_db):
    plan = legacy.plan(hub, legacy_db)
    assert plan.mapping["p1"] == plan.mapping["p2"] == "context-memory"
    assert plan.mapping["p3"] == "global" and plan.mapping["p5"] == "global"
    assert plan.mapping["p6"] == "context-memory", "same-named plain folder merges into the repo project"
    assert "같은 이름 폴더라 합침" in plan.describe()
    assert set(plan.new_projects) == {"context-memory"}, "projects with nothing to import are not registered"
    assert "가져올 기억이 없어" in plan.describe()
    assert plan.skipped == {"proposed": 1, "superseded": 1, "rejected": 1}
    with_proposed = legacy.plan(hub, legacy_db, ("active", "proposed"))
    assert set(with_proposed.new_projects) == {"context-memory", "deleted-dir"}
    separate = legacy.plan(hub, legacy_db, merge_by_name=False)
    assert separate.mapping["p6"] != "context-memory"
    assert hub.projects() == {}, "planning writes nothing"


def test_apply_imports_with_evidence_and_is_idempotent(hub, legacy_db):
    plan = legacy.plan(hub, legacy_db)
    assert legacy.apply(hub, legacy_db, plan) == 4
    pages = {p.meta["legacy_id"]: p for p in knowledge.iter_pages(hub)}
    assert set(pages) == {"m1", "m2", "m3", "m7"}
    assert pages["m7"].project == "context-memory"
    assert set(hub.projects()) == {"context-memory"}
    m1 = pages["m1"]
    assert m1.status == "active" and m1.project == "context-memory" and m1.meta["reviewed_by"] == "legacy"
    assert inbox.resolve_ref(hub, m1.sources[0])["text"] == "SQLite로 결정함 (근거)"
    assert pages["m2"].type == "fact" and pages["m3"].project == "global"
    assert legacy.apply(hub, legacy_db, legacy.plan(hub, legacy_db)) == 0
    assert len(list(knowledge.iter_pages(hub))) == 4


def test_include_proposed_creates_review_items(hub, legacy_db):
    plan = legacy.plan(hub, legacy_db, ("active", "proposed"))
    legacy.apply(hub, legacy_db, plan)
    assert [i.kind for i in review.pending(hub)] == ["new"]


def test_cli_preview_and_apply(hub, legacy_db, run_ctx):
    out = run_ctx("import-legacy", "--db", str(legacy_db)).stdout
    assert "미리보기" in out and not list(knowledge.iter_pages(hub))
    out = run_ctx("import-legacy", "--db", str(legacy_db), "--apply").stdout
    assert "4건을 가져왔습니다" in out


def test_legacy_dates_and_task_snapshots(hub, legacy_db):
    conn = sqlite3.connect(legacy_db)
    conn.execute("INSERT INTO memories (id, project_id, type, status, title, content, created_at, updated_at) "
                 "VALUES ('m8','p1','task','active','handoff — pushed','진행 스냅샷','2026-08-01','2026-08-05')")
    conn.commit()
    conn.close()
    legacy.apply(hub, legacy_db, legacy.plan(hub, legacy_db))
    page = next(p for p in knowledge.iter_pages(hub) if p.meta["legacy_id"] == "m8")
    assert page.meta["updated"] == "2026-08-05" and page.done is True
    from context_hub.recall import recall
    out = recall(hub, "할 일", project="context-memory").text
    assert "열린 할 일" not in out
