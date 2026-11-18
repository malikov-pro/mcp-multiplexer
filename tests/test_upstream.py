from __future__ import annotations

import httpx
import pytest

from mcp_multiplexer.config import BaseConfig
from mcp_multiplexer.upstream import BaseClient, UpstreamError


def _base(**overrides) -> BaseConfig:
    data = {
        "name": "admin_forum",
        "rpcUrl": "https://apigw.example.test/admin/forum/rpc",
        "headers": {"X-API-Key": "secret-key"},
        "maxConcurrent": 1,
    }
    data.update(overrides)
    return BaseConfig.model_validate(data)


@pytest.mark.asyncio
async def test_custom_headers_and_rpc_url() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = BaseClient(_base(), http)
        tools = await client.list_tools()

    assert tools == []
    assert str(seen[0].url) == "https://apigw.example.test/admin/forum/rpc"
    assert seen[0].headers["X-API-Key"] == "secret-key"
    assert "Authorization" not in seen[0].headers


@pytest.mark.asyncio
async def test_basic_auth_for_direct_publication() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        base = BaseConfig.model_validate(
            {
                "name": "data_rt_test",
                "url": "http://onec-ws:8080/retail_kz_test_malikov",
                "serviceRoot": "mcp",
                "username": "user",
                "password": "pass",
            }
        )
        client = BaseClient(base, http)
        await client.list_tools()

    assert str(seen[0].url) == "http://onec-ws:8080/retail_kz_test_malikov/hs/mcp/rpc"
    assert seen[0].headers["Authorization"].startswith("Basic ")


@pytest.mark.asyncio
async def test_json_rpc_error_raises() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "no"}},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = BaseClient(_base(), http)
        with pytest.raises(UpstreamError, match="JSON-RPC error"):
            await client.list_tools()


@pytest.mark.asyncio
async def test_non_json_response_raises() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = BaseClient(_base(), http)
        with pytest.raises(UpstreamError, match="HTTP 401"):
            await client.list_tools()
