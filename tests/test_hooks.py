import io
import json

import pytest

from context_hub import hooks, inbox, ops
from context_hub.hub import Hub, Project


@pytest.fixture
def svc(hub, tmp_path):
    path = tmp_path / "svc"
    path.mkdir()
    hub.add_project(Project("svc", name="Service", paths=[str(path)]))
    return path


def run(hub, action, payload, capsys):
    stdin = io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
    assert hooks.run(action, hub, stdin) == 0
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else None


def test_start_injects_now(hub, svc, capsys):
    assert run(hub, "start", {"cwd": str(svc), "hook_event_name": "SessionStart"}, capsys)["hookSpecificOutput"][
        "additionalContext"].startswith("Context Hub: 프로젝트 'Service'에 아직 NOW.md가 없습니다")
    ops.handoff(hub, cwd=str(svc), next_steps=["로그인 테스트 추가"])
    out = run(hub, "start", {"cwd": str(svc), "hook_event_name": "SessionStart"}, capsys)
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "로그인 테스트 추가" in out["hookSpecificOutput"]["additionalContext"]


def test_start_outside_projects_is_silent(hub, isolated_env, capsys):
    assert run(hub, "start", {"cwd": str(isolated_env)}, capsys) is None


def test_prompt_only_injects_when_relevant(hub, svc, capsys):
    ops.note(hub, "결정: 결제 실패 시 3회까지 자동 재시도한다", cwd=str(svc))
    assert run(hub, "prompt", {"cwd": str(svc), "prompt": "README 오타 고쳐줘"}, capsys) is None
    out = run(hub, "prompt", {"cwd": str(svc), "prompt": "결제 실패하면 재시도 몇 번이지?"}, capsys)
    assert "3회까지 자동 재시도" in out["hookSpecificOutput"]["additionalContext"]


def test_prompt_naming_other_project_injects(hub, svc, tmp_path, capsys):
    other = tmp_path / "other"
    other.mkdir()
    hub.add_project(Project("billing", name="B 프로젝트", aliases=["b"], paths=[str(other)]))
    ops.handoff(hub, cwd=str(other), next_steps=["환불 API"])
    out = run(hub, "prompt", {"cwd": str(svc), "prompt": "b프로젝트 할 일 알려줘"}, capsys)
    assert out is not None and "B 프로젝트" in out["hookSpecificOutput"]["additionalContext"]


def test_flush_ingests_given_transcript(hub, svc, isolated_env, capsys):
    transcript = isolated_env / ".claude" / "projects" / "-svc" / "s9.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({"type": "user", "sessionId": "s9", "cwd": str(svc),
                                      "message": {"role": "user", "content": "훅으로 수집"}}) + "\n")
    run(hub, "flush", {"transcript_path": str(transcript), "cwd": str(svc)}, capsys)
    texts = [e["text"] for f in inbox.files(hub) for e in inbox.read_lines(hub, f)]
    assert texts == ["훅으로 수집"]


@pytest.mark.parametrize("payload", ["", "{broken", "[]", json.dumps({"cwd": 5})])
def test_hooks_never_fail(hub, payload, capsys):
    for action in ("start", "prompt", "flush"):
        stdin = io.StringIO(payload)
        assert hooks.run(action, hub, stdin) == 0


def test_hooks_without_hub_do_nothing(tmp_path, capsys):
    assert hooks.run("start", Hub(tmp_path / "missing"), io.StringIO("{}")) == 0
    assert capsys.readouterr().out == ""


def test_prompt_with_long_instructions_still_finds_the_fact(hub, svc, capsys):
    ops.note(hub, "결정: 웹훅은 최대 5회 지수 백오프로 재시도", cwd=str(svc))
    for i in range(20):
        ops.note(hub, f"README 섹션 {i} 정리 메모", cwd=str(svc), kind="fact")
    prompt = "도구를 쓰지 말고, 이미 주어진 맥락만으로 답해: 이 프로젝트의 막힌 일과 웹훅 재시도 횟수는?"
    out = run(hub, "prompt", {"cwd": str(svc), "prompt": prompt}, capsys)
    assert out and "5회 지수 백오프" in out["hookSpecificOutput"]["additionalContext"]
    assert run(hub, "prompt", {"cwd": str(svc), "prompt": "이 함수 리팩터링하고 테스트도 추가해줘"}, capsys) is None


def test_hook_disable_env_prevents_recursion(hub, svc, capsys, monkeypatch):
    ops.handoff(hub, cwd=str(svc), next_steps=["x"])
    monkeypatch.setenv("CTX_HOOK_DISABLE", "1")
    assert run(hub, "start", {"cwd": str(svc)}, capsys) is None
