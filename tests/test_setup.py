import json
import os
import stat

import pytest

from context_hub import setup


@pytest.fixture
def fake_clients(isolated_env, tmp_path, monkeypatch):
    """Fake `claude`/`codex` CLIs that record their arguments."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    for name in ("claude", "codex"):
        script = bin_dir / name
        script.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\n')
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    (isolated_env / ".cursor").mkdir()
    return log


def test_plan_without_register_changes_nothing(hub, fake_clients, isolated_env, run_ctx):
    out = run_ctx("init").stdout
    assert "claude mcp add --scope user context-hub" in out and "아직 적용되지 않았습니다" in out
    assert not fake_clients.exists()
    assert not (isolated_env / ".claude").exists()


def test_register_is_idempotent_and_preserves_user_config(hub, fake_clients, isolated_env, run_ctx):
    settings = isolated_env / ".claude" / "settings.json"
    settings.parent.mkdir()
    user_hook = {"matcher": "", "hooks": [{"type": "command", "command": "my-own-hook.sh"}]}
    settings.write_text(json.dumps({"theme": "dark", "hooks": {"SessionStart": [user_hook]}}))
    (isolated_env / ".claude" / "CLAUDE.md").write_text("# 내 규칙\n- 존댓말\n")
    cursor = isolated_env / ".cursor" / "mcp.json"
    cursor.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))

    for _ in range(2):
        run_ctx("init", "--register")

    data = json.loads(settings.read_text())
    assert data["theme"] == "dark"
    start = data["hooks"]["SessionStart"]
    assert start[0] == user_hook and len(start) == 2
    for event in ("SessionStart", "UserPromptSubmit", "PreCompact", "SessionEnd"):
        ours = [h for e in data["hooks"][event] for h in e["hooks"] if " hook " in h["command"] and h["command"] != "my-own-hook.sh"]
        assert len(ours) == 1, event
    claude_md = (isolated_env / ".claude" / "CLAUDE.md").read_text()
    assert claude_md.startswith("# 내 규칙") and claude_md.count(setup.RULE_BEGIN) == 1
    assert (isolated_env / ".codex" / "AGENTS.md").read_text().count(setup.RULE_BEGIN) == 1
    servers = json.loads(cursor.read_text())["mcpServers"]
    assert servers["other"] == {"command": "x"} and servers["context-hub"]["args"][-1] == "serve"
    assert (settings.with_name("settings.json.context-hub.bak")).exists()
    calls = fake_clients.read_text().splitlines()
    assert calls.count(next(c for c in calls if c.startswith("claude mcp add"))) == 2
    assert any(c.startswith("codex mcp add context-hub -- ") and c.endswith(" serve") for c in calls)


def test_uninstall_removes_only_ours(hub, fake_clients, isolated_env, run_ctx):
    settings = isolated_env / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "keep.sh"}]}]}}))
    run_ctx("init", "--register")
    run_ctx("uninstall")
    data = json.loads(settings.read_text())
    assert data["hooks"] == {"Stop": [{"hooks": [{"type": "command", "command": "keep.sh"}]}]}
    assert not (isolated_env / ".claude" / "CLAUDE.md").exists(), "a file holding only our rule is removed"
    assert "hooks" in data
    assert "context-hub" not in json.loads((isolated_env / ".cursor" / "mcp.json").read_text())["mcpServers"]
    assert (isolated_env / "context-hub" / "projects.toml").exists(), "knowledge is kept"


def test_hub_is_a_git_repo_with_ignores(hub):
    assert hub.is_git()
    assert hub.git("log", "--oneline").stdout.strip()
    assert ".index/" in (hub.root / ".gitignore").read_text()
