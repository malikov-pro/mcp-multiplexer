from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
from mcp import types

from .config import BaseConfig

logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2024-11-05"
DEFAULT_TIMEOUT_SECONDS = 30.0


class UpstreamError(RuntimeError):
    """A base could not complete a JSON-RPC request."""


class BaseClient:
    """JSON-RPC client for one published 1C HTTP service.

    The router talks plain JSON-RPC to ``.../hs/<service_root>/rpc``; it does
    not need a full MCP client. Custom headers (for example ``X-API-Key`` to
    APISIX) and a full ``rpcUrl`` are supported, so the same class works with
    direct 1C publications and with token-protected APISIX routes.
    """

    def __init__(
        self,
        base: BaseConfig,
        http: httpx.AsyncClient,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.base = base
        self.http = http
        self.timeout = timeout
        self.rpc_url = base.resolved_rpc_url()
        self._semaphore = asyncio.Semaphore(max(1, base.max_concurrent))
        self._auth = base.auth()
        self._headers = dict(base.headers)

    async def rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params or {},
        }
        async with self._semaphore:
            try:
                response = await self.http.post(
                    self.rpc_url,
                    json=payload,
                    headers=self._headers,
                    auth=self._auth,
                    timeout=self.timeout,
                )
            except httpx.HTTPError as exc:
                raise UpstreamError(f"base '{self.base.name}': HTTP error: {exc}") from exc

        if response.status_code >= 400:
            raise UpstreamError(
                f"base '{self.base.name}': HTTP {response.status_code} from {self.rpc_url}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise UpstreamError(
                f"base '{self.base.name}': response is not JSON (HTTP {response.status_code})"
            ) from exc
        if not isinstance(data, dict):
            raise UpstreamError(f"base '{self.base.name}': JSON-RPC response is not an object")
        error = data.get("error")
        if error:
            raise UpstreamError(f"base '{self.base.name}': JSON-RPC error: {error}")
        return data.get("result")

    async def initialize_version(self) -> str | None:
        result = await self.rpc(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mcp-multiplexer", "version": "0.1.0"},
            },
        )
        if not isinstance(result, dict):
            return None
        server_info = result.get("serverInfo")
        if isinstance(server_info, dict):
            version = server_info.get("version")
            if isinstance(version, str):
                return version
        return None

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self.rpc("tools/list", {})
        if not isinstance(result, dict):
            raise UpstreamError(f"base '{self.base.name}': tools/list returned a non-object")
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise UpstreamError(f"base '{self.base.name}': tools/list has no tools array")
        for tool in tools:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                raise UpstreamError(f"base '{self.base.name}': malformed tool definition")
            if not isinstance(tool.get("inputSchema", {}), dict):
                raise UpstreamError(
                    f"base '{self.base.name}': tool '{tool.get('name')}' has a non-object inputSchema"
                )
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        result = await self.rpc("tools/call", {"name": name, "arguments": arguments})
        if isinstance(result, types.CallToolResult):
            return result
        if not isinstance(result, dict):
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=json.dumps(result, ensure_ascii=False, default=str),
                    )
                ]
            )
        try:
            return types.CallToolResult.model_validate(result)
        except Exception:  # noqa: BLE001 - upstream payload is untrusted
            # Some 1C services return a bare success object; do not lose it.
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=json.dumps(result, ensure_ascii=False, default=str),
                    )
                ]
            )


def create_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(DEFAULT_TIMEOUT_SECONDS),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        follow_redirects=False,
    )


def create_clients(bases: list[BaseConfig], http: httpx.AsyncClient) -> dict[str, BaseClient]:
    clients: dict[str, BaseClient] = {}
    for base in bases:
        clients[base.name] = BaseClient(base, http)
    return clients
