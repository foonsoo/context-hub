"""Client hook entry points: ``ctx hook start|prompt|flush`` (JSON on stdin).

Hooks are a convenience. They must never block or break the client, so every
failure is logged and the process exits 0.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from . import adapters, knowledge
from .hub import Hub
from .identity import resolve
from .recall import recall

START_BUDGET = 600
PROMPT_BUDGET = 1200


def _emit(event: str, text: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}},
                     ensure_ascii=False))


def _start(hub: Hub, payload: dict) -> None:
    cwd = payload.get("cwd") or os.getcwd()
    res = resolve(hub, cwd=cwd)
    if not res.slug:
        return
    projects = hub.projects()
    title = projects[res.slug].display if res.slug in projects else res.slug
    doc = knowledge.load_now(hub, res.slug, title)
    if doc.is_empty():
        text = f"Context Hub: 프로젝트 '{title}'에 아직 NOW.md가 없습니다. 작업을 마칠 때 handoff로 다음 할 일을 남겨 주세요."
    else:
        text = "Context Hub · 이전 세션에서 이어지는 상태입니다.\n" + doc.text().strip()
    from .text import estimate_tokens
    while estimate_tokens(text) > START_BUDGET and "\n" in text:
        text = text.rsplit("\n", 1)[0]
    _emit(payload.get("hook_event_name") or "SessionStart", text)


def _prompt(hub: Hub, payload: dict) -> None:
    prompt = (payload.get("prompt") or "").strip()
    if len(prompt) < 4:
        return
    cwd = payload.get("cwd")
    res = resolve(hub, cwd=cwd, text=prompt)
    other = res.how == "text" and bool(res.slugs) and res.slugs != [res.cwd_slug]
    # another project named: bring its NOW.md too; otherwise only relevant knowledge
    result = recall(hub, prompt, cwd=cwd, budget=PROMPT_BUDGET, include_now=other)
    relevant = bool(result.item_ids) and result.relevant
    named = other and (bool(result.item_ids) or "## 진행 상태" in result.text)
    if not (relevant or named):
        return  # never inject an empty "nothing found" block
    _emit(payload.get("hook_event_name") or "UserPromptSubmit",
          "Context Hub · 이 요청과 관련된 기록입니다 ([proposed]는 미검토):\n" + result.text)


def _flush(hub: Hub, payload: dict) -> None:
    transcript = payload.get("transcript_path")
    if transcript and Path(transcript).is_file():
        names = ["codex"] if "/.codex/" in transcript else ["claude-code"]
        adapters.ingest(hub, names=names, paths=[Path(transcript)])
    if os.environ.get("CTX_HOOK_NO_CURATE"):
        return
    # Curate in the background so the client is never kept waiting.
    subprocess.Popen(
        [sys.executable, "-m", "context_hub.cli", "curate", "--now", "--quiet"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, env={**os.environ, "CTX_HOME": str(hub.root)},
    )


def run(action: str, hub: Hub | None = None, stdin=None) -> int:
    if os.environ.get("CTX_HOOK_DISABLE"):
        return 0  # set for the curator's own Claude sessions
    hub = hub or Hub()
    try:
        raw = (stdin or sys.stdin).read()
        payload = json.loads(raw) if raw.strip() else {}
        if not hub.exists():
            return 0
        if action == "start":
            _start(hub, payload)
        elif action == "prompt":
            _prompt(hub, payload)
        elif action == "flush":
            _flush(hub, payload)
    except Exception as exc:
        try:
            hub.log(f"hook {action} failed: {exc!r}")
        except Exception:
            pass
    return 0
