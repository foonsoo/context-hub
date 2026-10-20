from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from context_hub.hub import Hub
from context_hub.identity import _case_fixed

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Every test gets its own HOME and hub; nothing touches the real machine."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CTX_HOME", str(home / "context-hub"))
    monkeypatch.setenv("CTX_CURATOR", "off")
    monkeypatch.setenv("CTX_HOOK_NO_CURATE", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "ANTHROPIC_API_KEY", "CTX_NOW"):
        monkeypatch.delenv(var, raising=False)
    (home / ".gitconfig").write_text("[user]\n\tname = Test\n\temail = test@example.com\n[init]\n\tdefaultBranch = main\n")
    _case_fixed.cache_clear()
    yield home


@pytest.fixture
def hub(isolated_env) -> Hub:
    from context_hub import setup

    hub = Hub()
    setup.init_hub(hub)
    return hub


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def make_repo(tmp_path):
    def make(name: str, remote: str | None = None, parent: Path | None = None) -> Path:
        path = (parent or tmp_path / "work") / name
        path.mkdir(parents=True)
        git(path, "init", "-q")
        if remote:
            git(path, "remote", "add", "origin", remote)
        (path / "README.md").write_text(name)
        git(path, "add", ".")
        git(path, "commit", "-q", "-m", "init")
        return path

    return make


@pytest.fixture
def run_ctx(isolated_env):
    """Run the real CLI in a subprocess (same interpreter, isolated env)."""

    def run(*args: str, cwd: Path | None = None, input: str | None = None, check: bool = True):
        result = subprocess.run(
            [sys.executable, "-m", "context_hub.cli", *args],
            cwd=cwd or isolated_env, input=input, capture_output=True, text=True,
            env={**os.environ}, timeout=60,
        )
        if check and result.returncode != 0:
            raise AssertionError(f"ctx {' '.join(args)} failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")
        return result

    return run
