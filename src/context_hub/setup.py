"""`ctx init`: create the hub and (optionally) register MCP clients, rules and hooks."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .hub import Hub

RULE_BEGIN = "<!-- context-hub:begin -->"
RULE_END = "<!-- context-hub:end -->"
RULE_TEXT = """## Context Hub
- 작업을 시작하면 context-hub의 `recall`을 사용자 요청으로 한 번 호출한다.
- 결정, 제약, 검증된 사실, 선호가 생기면 `note`로 남긴다. 비밀 정보는 남기지 않는다.
- 끝낼 때 `handoff`로 한 일과 다음 할 일을 남긴다."""
HOOK_MARK = "context-hub"


@dataclass
class Step:
    client: str
    action: str
    detail: str
    done: bool = False
    error: str | None = None


@dataclass
class SetupReport:
    steps: list[Step] = field(default_factory=list)

    def render(self, applied: bool) -> str:
        lines = []
        for step in self.steps:
            mark = "✓" if step.done else ("✗" if step.error else "·")
            line = f"{mark} [{step.client}] {step.action}: {step.detail}"
            if step.error:
                line += f"  ({step.error})"
            lines.append(line)
        if not applied:
            lines.append("\n위 변경은 아직 적용되지 않았습니다. `ctx init --register`로 적용합니다.")
        return "\n".join(lines)


def ctx_command() -> list[str]:
    found = shutil.which("ctx")
    if found:
        return [found]
    return [sys.executable, "-m", "context_hub.cli"]


def init_hub(hub: Hub) -> bool:
    created = not hub.exists()
    hub.ensure()
    if not hub.is_git() and shutil.which("git"):
        hub.git("init", "-q")
        hub.commit("init context hub")
    return created


def _wrap_rule() -> str:
    return f"{RULE_BEGIN}\n{RULE_TEXT}\n{RULE_END}\n"


def upsert_rule(path: Path) -> bool:
    """Insert or refresh the rule block. Returns True if the file changed."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    block = _wrap_rule()
    if RULE_BEGIN in text and RULE_END in text:
        start = text.index(RULE_BEGIN)
        end = text.index(RULE_END) + len(RULE_END)
        new = text[:start] + block.rstrip("\n") + text[end:]
    else:
        new = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block
    if new == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")
    return True


def remove_rule(path: Path) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    if RULE_BEGIN not in text:
        return False
    start = text.index(RULE_BEGIN)
    end = text.index(RULE_END) + len(RULE_END)
    path.write_text((text[:start].rstrip("\n") + "\n" + text[end:].lstrip("\n")).lstrip("\n"), encoding="utf-8")
    return True


def _backup(path: Path) -> None:
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".context-hub.bak"))


def _hook_entries(command: list[str]) -> dict[str, list[dict]]:
    base = " ".join(_quote(c) for c in command)

    def hook(action: str, timeout: int) -> dict:
        return {"type": "command", "command": f"{base} hook {action}", "timeout": timeout}

    return {
        "SessionStart": [{"matcher": "startup|resume|clear|compact", "hooks": [hook("start", 10)]}],
        "UserPromptSubmit": [{"hooks": [hook("prompt", 10)]}],
        "PreCompact": [{"hooks": [hook("flush", 20)]}],
        "SessionEnd": [{"hooks": [hook("flush", 20)]}],
    }


def _quote(part: str) -> str:
    return f'"{part}"' if " " in part else part


def merge_claude_hooks(settings_path: Path, command: list[str]) -> bool:
    data = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    before = json.dumps(data, sort_keys=True)
    hooks = data.setdefault("hooks", {})
    for event, entries in _hook_entries(command).items():
        kept = [e for e in hooks.get(event, []) if not any(_is_ours(h) for h in e.get("hooks", []))]
        hooks[event] = kept + entries
    changed = json.dumps(data, sort_keys=True) != before
    if changed:
        _backup(settings_path)
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return changed


def _is_ours(hook: dict) -> bool:
    command = hook.get("command", "")
    return "context_hub" in command or command.split(" hook ")[0].endswith("ctx")


def remove_claude_hooks(settings_path: Path) -> bool:
    if not settings_path.exists():
        return False
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    hooks = data.get("hooks") or {}
    changed = False
    for event in list(hooks):
        kept = [e for e in hooks[event] if not any(_is_ours(h) for h in e.get("hooks", []))]
        if kept != hooks[event]:
            changed = True
            hooks[event] = kept
        if not hooks[event]:
            del hooks[event]
    if changed:
        _backup(settings_path)
        settings_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return changed


def merge_mcp_json(path: Path, command: list[str]) -> bool:
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    servers = data.setdefault("mcpServers", {})
    entry = {"command": command[0], "args": [*command[1:], "serve"]}
    if servers.get("context-hub") == entry:
        return False
    _backup(path)
    servers["context-hub"] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def _run(argv: list[str]) -> str | None:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    if result.returncode != 0:
        return (result.stderr or result.stdout).strip()[:200] or f"exit {result.returncode}"
    return None


def plan_clients(clients: list[str] | None, hooks: bool = True) -> SetupReport:
    home = Path.home()
    command = ctx_command()
    serve = [*command, "serve"]
    report = SetupReport()
    wanted = set(clients or ["claude-code", "codex", "cursor"])
    claude_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    codex_dir = Path(os.environ.get("CODEX_HOME") or home / ".codex")
    if "claude-code" in wanted and shutil.which("claude"):
        report.steps.append(Step("claude-code", "MCP 등록", "claude mcp add --scope user context-hub -- " + " ".join(serve)))
        report.steps.append(Step("claude-code", "규칙", str(claude_dir / "CLAUDE.md")))
        if hooks:
            report.steps.append(Step("claude-code", "훅", str(claude_dir / "settings.json") + " (start/prompt/flush)"))
    elif "claude-code" in wanted:
        report.steps.append(Step("claude-code", "건너뜀", "claude 명령을 찾지 못했습니다"))
    if "codex" in wanted and shutil.which("codex"):
        report.steps.append(Step("codex", "MCP 등록", "codex mcp add context-hub -- " + " ".join(serve)))
        report.steps.append(Step("codex", "규칙", str(codex_dir / "AGENTS.md")))
    elif "codex" in wanted:
        report.steps.append(Step("codex", "건너뜀", "codex 명령을 찾지 못했습니다"))
    if "cursor" in wanted and (home / ".cursor").exists():
        report.steps.append(Step("cursor", "MCP 등록", str(home / ".cursor" / "mcp.json")))
    elif "cursor" in wanted:
        report.steps.append(Step("cursor", "건너뜀", "~/.cursor 폴더가 없습니다"))
    return report


def apply_clients(report: SetupReport) -> SetupReport:
    home = Path.home()
    command = ctx_command()
    claude_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    codex_dir = Path(os.environ.get("CODEX_HOME") or home / ".codex")
    for step in report.steps:
        try:
            if step.action == "건너뜀":
                continue
            if step.client == "claude-code" and step.action == "MCP 등록":
                _run(["claude", "mcp", "remove", "--scope", "user", "context-hub"])
                step.error = _run(["claude", "mcp", "add", "--scope", "user", "context-hub", "--", *command, "serve"])
            elif step.client == "claude-code" and step.action == "규칙":
                upsert_rule(claude_dir / "CLAUDE.md")
            elif step.client == "claude-code" and step.action == "훅":
                merge_claude_hooks(claude_dir / "settings.json", command)
            elif step.client == "codex" and step.action == "MCP 등록":
                _run(["codex", "mcp", "remove", "context-hub"])
                step.error = _run(["codex", "mcp", "add", "context-hub", "--", *command, "serve"])
            elif step.client == "codex" and step.action == "규칙":
                upsert_rule(codex_dir / "AGENTS.md")
            elif step.client == "cursor":
                merge_mcp_json(home / ".cursor" / "mcp.json", command)
            step.done = step.error is None
        except Exception as exc:
            step.error = str(exc)
    return report


def unregister(clients: list[str] | None = None) -> SetupReport:
    home = Path.home()
    report = SetupReport()
    wanted = set(clients or ["claude-code", "codex", "cursor"])
    claude_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    codex_dir = Path(os.environ.get("CODEX_HOME") or home / ".codex")
    if "claude-code" in wanted:
        if shutil.which("claude"):
            error = _run(["claude", "mcp", "remove", "--scope", "user", "context-hub"])
            report.steps.append(Step("claude-code", "MCP 해제", "claude mcp remove", done=error is None, error=error))
        report.steps.append(Step("claude-code", "규칙 제거", str(claude_dir / "CLAUDE.md"), done=remove_rule(claude_dir / "CLAUDE.md")))
        report.steps.append(Step("claude-code", "훅 제거", str(claude_dir / "settings.json"), done=remove_claude_hooks(claude_dir / "settings.json")))
    if "codex" in wanted:
        if shutil.which("codex"):
            error = _run(["codex", "mcp", "remove", "context-hub"])
            report.steps.append(Step("codex", "MCP 해제", "codex mcp remove", done=error is None, error=error))
        report.steps.append(Step("codex", "규칙 제거", str(codex_dir / "AGENTS.md"), done=remove_rule(codex_dir / "AGENTS.md")))
    if "cursor" in wanted:
        path = home / ".cursor" / "mcp.json"
        changed = False
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if (data.get("mcpServers") or {}).pop("context-hub", None) is not None:
                _backup(path)
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                changed = True
        report.steps.append(Step("cursor", "MCP 해제", str(path), done=changed))
    return report
