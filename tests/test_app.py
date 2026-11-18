from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

import mcp_multiplexer.app as app_module
from mcp_multiplexer.app import create_app

TOOL = {
    "name": "execute_query",
    "description": "Run a query",
    "inputSchema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
}


async def _upstream(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    method = body.get("method")
    if method == "initialize":
        result: dict[str, Any] = {"serverInfo": {"version": "1.6.3"}}
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "ok"}]}
    else:
        return httpx.Response(404)
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"), "result": result})


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    config = {
        "name": "data-farm",
        "groupBy": "base",
        "reference": "rt",
        "bases": [
            {
                "name": "rt",
                "value": "rt",
                "url": "http://mock",
                "username": "user",
                "password": "pass",
            }
        ],
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    def _mock_http() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(_upstream))

    monkeypatch.setattr(app_module, "create_http_client", _mock_http)
    app = create_app(str(config_path))
    with TestClient(app) as test_client:
        yield test_client


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "data-farm"
    assert data["tools"] == 1
    assert data["members"][0]["status"] == "reference"


def test_mcp_initialize_and_list_and_call(client: TestClient) -> None:
    headers = {"Accept": "application/json, text/event-stream"}
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }
    response = client.post("/mcp", json=initialize, headers=headers)
    assert response.status_code == 200
    assert response.json()["result"]["serverInfo"]["name"] == "mcp-multiplexer"

    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        headers=headers,
    )
    assert response.status_code == 200
    tools = response.json()["result"]["tools"]
    assert tools[0]["name"] == "execute_query"
    assert tools[0]["inputSchema"]["properties"]["base"]["enum"] == ["rt"]

    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "execute_query",
                "arguments": {"query": "SELECT 1", "base": "rt"},
            },
        },
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["result"]["content"][0]["text"] == "ok"
