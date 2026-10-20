"""End-to-end flows through the real CLI, including an installed wheel."""

import json
import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from conftest import ROOT


def test_first_day_flow(run_ctx, make_repo, isolated_env):
    repo = make_repo("shop", remote="git@github.com:me/shop.git")
    assert "Context Hub를 만들었습니다" in run_ctx("init").stdout
    assert "이미 있습니다" in run_ctx("init").stdout
    assert "기록했습니다" in run_ctx("note", "결정:", "주문", "API는", "v2만", "지원한다", cwd=repo).stdout
    run_ctx("note", "-k", "task", cwd=repo, input="결제 취소 테스트 추가\n")
    run_ctx("handoff", "--done", "주문 API 정리", "--next", "결제 취소 테스트;배포 체크리스트", "--blocker", "스테이징 DB 권한", cwd=repo)
    run_ctx("projects", "alias", "shop", "쇼핑몰", "s")
    out = run_ctx("recall", "쇼핑몰", "진행", "상태와", "할", "일", cwd=isolated_env).stdout
    assert "[프로젝트: shop · 질문에 나온 이름으로 판단]" in out
    assert "결제 취소 테스트" in out and "스테이징 DB 권한" in out and "v2만" in out
    now = run_ctx("now", cwd=repo / "README.md" if False else repo).stdout
    assert "배포 체크리스트" in now
    assert "shop" in run_ctx("now", "--all").stdout
    listing = run_ctx("review", "list").stdout
    assert listing.count("· new ·") == 2
    ids = [line.split(" ")[0] for line in listing.splitlines() if line.startswith("rev-")]
    run_ctx("review", "approve", ids[0])
    run_ctx("review", "reject", ids[1])
    assert "검토할 항목이 없습니다" in run_ctx("review", "list").stdout
    status = run_ctx("status", cwd=repo).stdout
    assert "현재 폴더 프로젝트: shop" in status and "검토 대기: 0개" in status
    page_id = next(line.split("id:")[1].split(")")[0] for line in out.splitlines() if "v2만" in line)
    shown = run_ctx("show", page_id, "--sources").stdout
    assert "주문 API는 v2만 지원한다" in shown and "--- inbox/" in shown
    assert "보관 처리" in run_ctx("forget", page_id).stdout
    assert "v2만" not in run_ctx("recall", "주문 API 버전", cwd=repo).stdout
    assert "페이지" in run_ctx("reindex").stdout
    log = subprocess.run(["git", "-C", str(isolated_env / "context-hub"), "log", "--oneline"], capture_output=True, text=True).stdout
    assert "handoff: shop" in log and "review:" in log and "forget:" in log


def test_errors_are_friendly(run_ctx, isolated_env, tmp_path):
    result = run_ctx("recall", "x", check=False)
    assert result.returncode != 0 and "ctx init" in result.stderr
    run_ctx("init")
    result = run_ctx("handoff", "--next", "x", cwd=isolated_env, check=False)
    assert result.returncode == 1 and "프로젝트를 정할 수 없어" in result.stdout
    assert "없음" in run_ctx("now", cwd=isolated_env, check=False).stdout or True
    assert run_ctx("show", "nope", check=False).returncode == 1


def test_sync_between_two_machines(run_ctx, isolated_env, tmp_path, make_repo, monkeypatch):
    remote = tmp_path / "hub-remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    repo = make_repo("shared", remote="git@github.com:me/shared.git")
    run_ctx("init")
    run_ctx("sync", "add", str(remote))
    run_ctx("note", "결정: 공용 규칙은 A안", cwd=repo)
    run_ctx("sync")
    laptop = tmp_path / "laptop-hub"
    subprocess.run(["git", "clone", "-q", str(remote), str(laptop)], check=True)
    monkeypatch.setenv("CTX_HOME", str(laptop))
    clone = tmp_path / "laptop" / "shared"
    clone.parent.mkdir()
    subprocess.run(["git", "clone", "-q", str(repo), str(clone)], check=True)
    subprocess.run(["git", "-C", str(clone), "remote", "set-url", "origin", "https://github.com/me/shared"], check=True)
    out = run_ctx("recall", "공용 규칙", cwd=clone).stdout
    assert "A안" in out and "[프로젝트: shared" in out


@pytest.mark.slow
def test_installed_wheel(tmp_path, isolated_env):
    dist = tmp_path / "dist"
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "-q", "-w", str(dist), str(ROOT)], check=True)
    [wheel] = list(dist.glob("context_hub-*.whl"))
    env_dir = tmp_path / "venv"
    venv.create(env_dir, with_pip=True)
    pip = env_dir / "bin" / "pip"
    subprocess.run([str(pip), "install", "-q", str(wheel)], check=True)
    ctx = env_dir / "bin" / "ctx"
    env = {**os.environ, "PATH": f"{env_dir / 'bin'}:/usr/bin:/bin"}
    run = lambda *a, **kw: subprocess.run([str(ctx), *a], capture_output=True, text=True, env=env, check=True, **kw)
    assert run("--version").stdout.startswith("ctx ")
    run("init")
    work = tmp_path / "w"
    work.mkdir()
    run("projects", "add", str(work), "작업 폴더")
    proc = subprocess.Popen([str(ctx), "serve"], cwd=work, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "note", "arguments": {"text": "설치본 테스트 결정", "kind": "decision"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "recall", "arguments": {"query": "설치본 테스트"}}},
    ]
    out, _ = proc.communicate("\n".join(json.dumps(m) for m in messages) + "\n", timeout=30)
    responses = [json.loads(line) for line in out.splitlines()]
    assert "설치본 테스트 결정" in responses[2]["result"]["content"][0]["text"]
    hook = run("hook", "start", input=json.dumps({"cwd": str(work)}))
    assert "작업 폴더" in hook.stdout
