import json
from pathlib import Path

import pytest

from context_hub import adapters, inbox
from context_hub.adapters import claude_code, codex
from context_hub.hub import Project


def write_jsonl(path: Path, rows, partial: str | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        if partial:
            handle.write(partial)


def cc_user(text, cwd, **extra):
    return {"type": "user", "sessionId": "s1", "cwd": cwd, "timestamp": "2026-10-21T01:00:00Z",
            "message": {"role": "user", "content": text}, **extra}


def cc_assistant(blocks, cwd):
    return {"type": "assistant", "sessionId": "s1", "cwd": cwd, "timestamp": "2026-10-21T01:00:01Z",
            "message": {"role": "assistant", "content": blocks}}


@pytest.fixture
def svc(hub, tmp_path):
    path = tmp_path / "svc"
    path.mkdir()
    hub.add_project(Project("svc", paths=[str(path)]))
    return path


@pytest.fixture
def claude_home(isolated_env):
    return isolated_env / ".claude" / "projects" / "-svc"


def events(hub):
    return [e.data for f in inbox.files(hub) for e in inbox.read_lines(hub, f)]


def test_claude_code_filters_noise_and_keeps_conversation(hub, svc, claude_home):
    cwd = str(svc)
    write_jsonl(claude_home / "s1.jsonl", [
        {"type": "queue-operation", "sessionId": "s1"},
        cc_user("<system-reminder>ignore me</system-reminder>결제 재시도 5회로 바꿔줘", cwd),
        cc_user("<command-name>/clear</command-name>", cwd),
        cc_user([{"type": "tool_result", "content": "big output"}], cwd),
        cc_user("subagent prompt", cwd, isSidechain=True),
        cc_assistant([{"type": "thinking", "thinking": "secret thoughts"},
                      {"type": "text", "text": "5회로 바꿨습니다."},
                      {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q tests/test_retry.py"}}], cwd),
        {"type": "attachment", "sessionId": "s1"},
        "not even a dict",
    ], partial='{"type": "user", "message": {"content": "half wri')
    result = adapters.ingest(hub, names=["claude-code"])
    assert result == {"claude-code": 3}
    got = [(e["role"], e["text"]) for e in events(hub)]
    assert got == [
        ("user", "결제 재시도 5회로 바꿔줘"),
        ("assistant", "5회로 바꿨습니다."),
        ("tool", "tool Bash: pytest -q tests/test_retry.py"),
    ]
    assert all(e["project"] == "svc" and e["client"] == "claude-code" for e in events(hub))
    # finishing the partial line later picks it up exactly once
    with open(claude_home / "s1.jsonl", "a") as handle:
        handle.write('tten"}, "sessionId": "s1", "cwd": "%s"}\n' % cwd)
    assert adapters.ingest(hub, names=["claude-code"]) == {"claude-code": 1}
    assert adapters.ingest(hub, names=["claude-code"]) == {"claude-code": 0}


def test_rewritten_file_starts_over(hub, svc, claude_home):
    path = claude_home / "s1.jsonl"
    write_jsonl(path, [cc_user("첫 번째 긴 메시지입니다", str(svc))])
    adapters.ingest(hub, names=["claude-code"])
    path.write_text(json.dumps(cc_user("짧게", str(svc))) + "\n")
    assert adapters.ingest(hub, names=["claude-code"]) == {"claude-code": 1}


def test_capture_off_and_unknown_folders(hub, svc, claude_home, tmp_path):
    hub.update_project("svc", capture=False)
    write_jsonl(claude_home / "s1.jsonl", [cc_user("수집하면 안 됨", str(svc))])
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    write_jsonl(claude_home / "s2.jsonl", [dict(cc_user("임시 폴더", str(scratch)), sessionId="s2")])
    adapters.ingest(hub, names=["claude-code"])
    texts = [e["text"] for e in events(hub)]
    assert texts == ["임시 폴더"]
    assert events(hub)[0]["project"] is None
    assert set(hub.projects()) == {"svc"}, "scratch folders are not auto-registered"


def test_git_repo_is_auto_registered(hub, claude_home, make_repo):
    repo = make_repo("auto", remote="git@github.com:me/auto.git")
    write_jsonl(claude_home / "s1.jsonl", [cc_user("새 저장소에서 작업", str(repo))])
    adapters.ingest(hub, names=["claude-code"])
    assert events(hub)[0]["project"] == "auto"


def test_codex_wrapped_and_legacy_formats(hub, svc, isolated_env):
    root = isolated_env / ".codex" / "sessions" / "2026" / "10" / "21"
    write_jsonl(root / "rollout-2026-10-21-a.jsonl", [
        {"timestamp": "t", "type": "session_meta", "payload": {"id": "codex-1", "cwd": str(svc)}},
        {"timestamp": "t", "type": "response_item", "payload": {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "<environment_context>cwd</environment_context>"}]}},
        {"timestamp": "t", "type": "response_item", "payload": {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "로그인 버그 고쳐줘"}]}},
        {"timestamp": "t", "type": "response_item", "payload": {"type": "function_call", "name": "shell",
         "arguments": json.dumps({"command": ["pytest", "-q"]})}},
        {"timestamp": "t", "type": "response_item", "payload": {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "고쳤습니다"}]}},
        {"timestamp": "t", "type": "event_msg", "payload": {"type": "token_count"}},
    ])
    write_jsonl(root / "rollout-2026-10-21-b.jsonl", [
        {"id": "legacy-1", "timestamp": "t", "instructions": "", "cwd": str(svc)},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "예전 형식"}]},
    ])
    assert adapters.ingest(hub, names=["codex"]) == {"codex": 4}
    got = sorted((e["session"], e["role"], e["text"]) for e in events(hub))
    assert got == [
        ("codex-1", "assistant", "고쳤습니다"),
        ("codex-1", "tool", 'tool shell: ["pytest", "-q"]'),
        ("codex-1", "user", "로그인 버그 고쳐줘"),
        ("legacy-1", "user", "예전 형식"),
    ]


def test_bad_file_does_not_stop_other_files(hub, svc, claude_home, monkeypatch):
    write_jsonl(claude_home / "a.jsonl", [cc_user("정상", str(svc))])
    write_jsonl(claude_home / "b.jsonl", [cc_user("정상2", str(svc))])
    original = claude_code.parse

    def flaky(path, offset):
        if path.name == "a.jsonl":
            raise OSError("boom")
        return original(path, offset)

    monkeypatch.setattr(claude_code.ADAPTER, "parse", flaky)
    assert adapters.ingest(hub, names=["claude-code"]) == {"claude-code": 1}


def test_discover_respects_env(monkeypatch, tmp_path):
    (tmp_path / "cfg" / "projects" / "x").mkdir(parents=True)
    (tmp_path / "cfg" / "projects" / "x" / "s.jsonl").write_text("")
    (tmp_path / "cfg" / "projects" / "x" / "s" / "subagents").mkdir(parents=True)
    (tmp_path / "cfg" / "projects" / "x" / "s" / "subagents" / "y.jsonl").write_text("")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert [p.name for p in claude_code.discover()] == ["s.jsonl"]
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nothing"))
    assert codex.discover() == []


@pytest.mark.parametrize("raw,expected", [
    ("<recommended_plugins>\nHere are plugins\n</recommended_plugins>", ""),
    ("The following is the Codex agent history whose request action you are assessing.", ""),
    ("Call these context_memory tools first", ""),
    ("# Files mentioned by the user:\n\n## a.pdf: /tmp/a.pdf\n\n## My request for Codex:\n\n월별 명세서 정리해줘\n<image name=[Image #1] path=\"/tmp/x\">", "월별 명세서 정리해줘"),
    ("# Files mentioned by the user:\n\n## a.png: /tmp/a.png\n\n## My request:\n<image name=[Image #1] path=\"/tmp/x\"></image>", ""),
    ("<b>굵게</b> 이건 실제 질문", "<b>굵게</b> 이건 실제 질문"),
])
def test_injected_user_text_is_cleaned(raw, expected):
    from context_hub.adapters._common import clean_user_text
    assert clean_user_text(raw) == expected


def test_curator_prompt_collapses_tool_runs(hub):
    from context_hub import curator
    file = inbox.day_dir(hub) / "codex-t.jsonl"
    inbox.append(hub, file, [{"role": "user", "text": "테스트 돌려줘"}] +
                 [{"role": "tool", "text": f"tool shell: step {i}"} for i in range(12)] +
                 [{"role": "assistant", "text": "끝"}])
    entries = list(inbox.read_lines(hub, file))
    prompt = curator.build_prompt("p", "P", entries, [], "")
    assert "L2 tools: shell: step 0; shell: step 1" in prompt and "… +4" in prompt
    assert "L14 assistant: 끝" in prompt
