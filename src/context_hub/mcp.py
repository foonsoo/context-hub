"""Minimal MCP stdio server exposing recall / note / handoff (no dependencies)."""

from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any, Callable, TextIO

from . import __version__, ops
from .hub import Hub
from .knowledge import TYPES
from .recall import recall

SUPPORTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = (
    "Context Hub shares context across sessions and AI clients. Always pass `cwd` (the absolute "
    "path of the current working directory). At the start of a task call "
    "`recall` once with the user's request. When a decision, constraint, verified fact or "
    "preference appears, call `note`. Before finishing, call `handoff` with what was done and "
    "what comes next. Never store secrets. Items marked [proposed] are unreviewed."
)
PROJECT_PROP = {
    "type": "string",
    "description": "Project name or alias. Omit to infer from the question/working directory. '*' = all projects (recall only).",
}
CWD_PROP = {"type": "string", "description": "Absolute path of the current working directory. Always pass it; the server's own directory may differ."}
LIST_PROP = {"type": "array", "items": {"type": "string"}}

TOOLS = [
    {
        "name": "recall",
        "description": (
            "Read shared context: the project's NOW.md (current state, next steps) and related "
            "knowledge with sources. Call once at task start with the user's request as query. "
            "Mentioning another project by name (e.g. 'B 프로젝트 상태') reads that project."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The user's request or a focused question."},
                "cwd": CWD_PROP,
                "project": PROJECT_PROP,
                "budget": {"type": "integer", "minimum": 200, "maximum": 20000,
                           "description": "Approximate token budget for the answer (default 2000)."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "note",
        "description": (
            "Save one durable piece of knowledge (decision, constraint, verified fact, procedure, "
            "preference, open task). It is stored as 'proposed' until the user reviews it. "
            "Do not save secrets or routine chatter."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The knowledge, self-contained, 1-6 sentences."},
                "kind": {"type": "string", "enum": sorted(TYPES)},
                "cwd": CWD_PROP,
                "project": {"type": "string", "description": "Project name or alias. Omit to use the working directory."},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "handoff",
        "description": (
            "Update the project's NOW.md for the next session: what was done, what comes next, "
            "blockers. Call before finishing a task."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "done": {**LIST_PROP, "description": "Completed items."},
                "next": {**LIST_PROP, "description": "Next steps, most important first. Replaces the previous list."},
                "blockers": {**LIST_PROP, "description": "Current blockers. Pass [] to clear."},
                "in_progress": {**LIST_PROP, "description": "Work still in progress."},
                "goal": {"type": "string", "description": "Current goal, if it changed."},
                "cwd": CWD_PROP,
                "project": {"type": "string", "description": "Project name or alias. Omit to use the working directory."},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    },
]


class ToolError(Exception):
    pass


def _check(args: dict, schema: dict) -> None:
    props = schema.get("properties", {})
    missing = [k for k in schema.get("required", []) if k not in args]
    if missing:
        raise ToolError(f"missing required argument(s): {', '.join(missing)}")
    extra = sorted(set(args) - set(props))
    if extra:
        raise ToolError(f"unknown argument(s): {', '.join(extra)}")
    for key, value in args.items():
        kind = props[key].get("type")
        ok = {
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "array": isinstance(value, list) and all(isinstance(v, str) for v in value),
        }.get(kind, True)
        if not ok:
            raise ToolError(f"argument '{key}' must be {kind}")
        if "enum" in props[key] and value not in props[key]["enum"]:
            raise ToolError(f"argument '{key}' must be one of {props[key]['enum']}")


class Server:
    def __init__(self, hub: Hub, client_name: str = "mcp"):
        self.hub = hub
        self.client_name = client_name
        self.default_cwd = os.getcwd()
        self.handlers: dict[str, Callable[[dict], dict]] = {
            "recall": self._recall, "note": self._note, "handoff": self._handoff,
        }

    def _cwd(self, args: dict) -> str:
        return args.get("cwd") or self.default_cwd

    def _recall(self, args: dict) -> dict:
        result = recall(self.hub, args["query"], cwd=self._cwd(args), project=args.get("project"),
                        budget=args.get("budget") or 2000)
        return {"text": result.text}

    def _note(self, args: dict) -> dict:
        result = ops.note(self.hub, args["text"], cwd=self._cwd(args), kind=args.get("kind"),
                          project=args.get("project"), client=self.client_name)
        if not result.ok:
            raise ToolError(result.message)
        return {"text": result.message}

    def _handoff(self, args: dict) -> dict:
        result = ops.handoff(self.hub, cwd=self._cwd(args), done=args.get("done"),
                             next_steps=args.get("next"), blockers=args.get("blockers"),
                             goal=args.get("goal"), in_progress=args.get("in_progress"),
                             project=args.get("project"), client=self.client_name)
        if not result.ok:
            raise ToolError(result.message)
        return {"text": result.message}

    def handle(self, message: dict) -> dict | None:
        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params") or {}
        if msg_id is None:  # notification
            return None
        try:
            if method == "initialize":
                info = params.get("clientInfo") or {}
                if info.get("name"):
                    self.client_name = str(info["name"])
                requested = params.get("protocolVersion")
                version = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
                result = {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "context-hub", "version": __version__},
                    "instructions": INSTRUCTIONS,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                name = params.get("name")
                args = params.get("arguments") or {}
                tool = next((t for t in TOOLS if t["name"] == name), None)
                if not tool:
                    return _error(msg_id, -32602, f"unknown tool: {name}")
                try:
                    _check(args, tool["inputSchema"])
                    out = self.handlers[name](args)
                    result = {"content": [{"type": "text", "text": out["text"]}], "isError": False}
                except ToolError as exc:
                    result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
            elif method in ("resources/list", "prompts/list"):
                result = {method.split("/")[0]: []}
            else:
                return _error(msg_id, -32601, f"method not found: {method}")
        except Exception as exc:  # keep the server alive; report the failure
            self.hub.log("mcp error: " + traceback.format_exc())
            return _error(msg_id, -32603, f"internal error: {exc}")
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def serve(hub: Hub, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
    hub.ensure()
    server = Server(hub)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response = _error(None, -32700, "parse error")
        else:
            if isinstance(message, list):
                responses = [r for r in (server.handle(m) for m in message if isinstance(m, dict)) if r]
                if responses:
                    stdout.write(json.dumps(responses, ensure_ascii=False) + "\n")
                    stdout.flush()
                continue
            response = server.handle(message) if isinstance(message, dict) else _error(None, -32600, "invalid request")
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()
