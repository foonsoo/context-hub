import json
import os
import subprocess
import sys

import pytest

from context_hub.hub import Project


class Client:
    def __init__(self, cwd):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "context_hub.cli", "serve"], cwd=cwd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ},
        )
        self.next_id = 0

    def send_raw(self, line: str):
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def read(self):
        line = self.proc.stdout.readline()
        assert line, self.proc.stderr.read()
        return json.loads(line)

    def call(self, method, params=None):
        self.next_id += 1
        self.send_raw(json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}))
        response = self.read()
        assert response["id"] == self.next_id
        return response

    def tool(self, name, **arguments):
        return self.call("tools/call", {"name": name, "arguments": arguments})["result"]

    def close(self):
        self.proc.stdin.close()
        assert self.proc.wait(10) == 0


@pytest.fixture
def client(hub, tmp_path):
    work = tmp_path / "svc"
    work.mkdir()
    hub.add_project(Project("svc", name="Service", aliases=["서비스"], paths=[str(work)]))
    c = Client(work)
    yield c
    c.close()


def test_handshake_and_tool_list(client):
    init = client.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "claude-code", "version": "x"}})
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert init["result"]["serverInfo"]["name"] == "context-hub"
    assert "recall" in init["result"]["instructions"]
    client.send_raw(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
    tools = client.call("tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["recall", "note", "handoff"]
    assert all(t["inputSchema"]["additionalProperties"] is False for t in tools)
    assert client.call("ping")["result"] == {}


def test_unknown_protocol_gets_latest(client):
    assert client.call("initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"]


def test_note_then_recall_uses_server_cwd(client):
    client.call("initialize", {"protocolVersion": "2025-06-18"})
    result = client.tool("note", text="결정: 배포는 화요일에만 한다")
    assert result["isError"] is False and "svc" in result["content"][0]["text"]
    result = client.tool("handoff", done=["배포 규칙 정리"], next=["배포 스크립트 수정"])
    assert result["isError"] is False
    text = client.tool("recall", query="배포 언제 하기로 했지")["content"][0]["text"]
    assert "화요일" in text and "배포 스크립트 수정" in text


def test_errors_are_tool_errors_not_crashes(client):
    bad = client.tool("note", text="x", kind="nonsense")
    assert bad["isError"] and "kind" in bad["content"][0]["text"]
    bad = client.tool("recall", query="x", extra=1)
    assert bad["isError"] and "unknown argument" in bad["content"][0]["text"]
    bad = client.tool("recall")
    assert bad["isError"] and "query" in bad["content"][0]["text"]
    bad = client.tool("handoff", next="not a list")
    assert bad["isError"]
    bad = client.tool("handoff", project="없는프로젝트", next=["x"])
    assert bad["isError"] and "찾지 못했습니다" in bad["content"][0]["text"]
    assert "error" in client.call("tools/call", {"name": "nope", "arguments": {}})
    assert client.call("bogus/method")["error"]["code"] == -32601
    client.send_raw("{not json")
    assert client.read()["error"]["code"] == -32700
    assert client.call("ping")["result"] == {}, "server survives every error above"


def test_batch_requests(client):
    client.send_raw(json.dumps([
        {"jsonrpc": "2.0", "id": "a", "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": "b", "method": "tools/list"},
    ]))
    responses = client.read()
    assert [r["id"] for r in responses] == ["a", "b"]


def test_explicit_project_argument(client, hub):
    client.tool("note", text="서비스 포트는 8080", project="서비스", kind="fact")
    text = client.tool("recall", query="포트 번호", project="svc")["content"][0]["text"]
    assert "8080" in text
