from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import httpx
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import __version__
from .catalog import CatalogError, MultiplexedCatalog, build_catalog
from .config import MultiplexerConfig, config_path_from_env, load_config
from .fanout import fan_out_call
from .upstream import BaseClient, create_clients, create_http_client

logger = logging.getLogger(__name__)


class AppState:
    def __init__(self, config: MultiplexerConfig) -> None:
        self.config = config
        self.http: httpx.AsyncClient | None = None
        self.clients: dict[str, BaseClient] = {}
        self.catalog: MultiplexedCatalog | None = None


class MCPASGIApp:
    """Minimal ASGI wrapper around the SDK session manager."""

    def __init__(self, manager: StreamableHTTPSessionManager) -> None:
        self.manager = manager

    async def __call__(self, scope, receive, send) -> None:
        await self.manager.handle_request(scope, receive, send)


def _error_result(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)],
        isError=True,
    )


def create_app(config_path: str | None = None) -> Starlette:
    config = load_config(config_path or config_path_from_env())
    state = AppState(config)

    server = Server(
        "mcp-multiplexer",
        version=__version__,
        instructions=(
            f"Strict multiplexer '{config.name}'. Every tool has a required "
            f"'{config.group_by}' parameter; only compatible members are listed."
        ),
    )

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        if state.catalog is None:
            return []
        return state.catalog.tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
        catalog = state.catalog
        if catalog is None:
            return _error_result("catalog is not ready")

        if name not in catalog.tool_values:
            return _error_result(
                f"tool '{name}' is not available; available: {', '.join(catalog.tool_values)}"
            )

        args = dict(arguments or {})
        if config.group_by not in args:
            return _error_result(
                f"parameter '{config.group_by}' is required; "
                f"valid values: {', '.join(catalog.tool_values[name])}"
            )
        selector = args.pop(config.group_by)
        if not isinstance(selector, str):
            return _error_result(f"parameter '{config.group_by}' must be a string")

        if selector == "all":
            if not config.fan_out.enabled or name not in config.fan_out.tools:
                return _error_result(f"fan-out ('all') is not enabled for tool '{name}'")
            try:
                return await fan_out_call(catalog, state.clients, name, args)
            except Exception as exc:
                logger.exception("fan-out failed for tool %s", name)
                return _error_result(f"fan-out failed: {exc}")

        member = catalog.member_by_selector(selector)
        if member is None:
            return _error_result(
                f"unknown target '{selector}' for tool '{name}'; "
                f"valid values: {', '.join(catalog.tool_values[name])}"
            )

        client = state.clients.get(member.name)
        if client is None:
            return _error_result(f"member '{member.name}' has no client")

        logger.info("tool call tool=%s target=%s server=%s", name, selector, member.name)
        try:
            return await client.call_tool(name, args)
        except Exception as exc:  # noqa: BLE001 - upstream failure is a tool error
            logger.warning("upstream call failed tool=%s target=%s: %s", name, selector, exc)
            return _error_result(f"member '{member.name}': {exc}")

    manager = StreamableHTTPSessionManager(
        app=server,
        stateless=True,
        json_response=True,
    )

    @asynccontextmanager
    async def lifespan(_: Starlette):
        http = create_http_client()
        state.http = http
        state.clients = create_clients(config.bases, http)
        try:
            state.catalog = await build_catalog(config, state.clients)
            logger.info(
                "catalog ready name=%s reference=%s tools=%s members=%s",
                config.name,
                state.catalog.reference.name,
                len(state.catalog.tools),
                len(state.catalog.members),
            )
            async with manager.run():
                yield
        finally:
            await http.aclose()
            state.http = None

    async def health(_: Request) -> JSONResponse:
        if state.catalog is None:
            return JSONResponse({"status": "starting"}, status_code=503)
        return JSONResponse(state.catalog.health())

    app = Starlette(
        routes=[
            Route("/health", endpoint=health),
            Route("/mcp", endpoint=MCPASGIApp(manager)),
        ],
        lifespan=lifespan,
    )
    app.state.multiplexer = state
    return app


def create_app_from_env() -> Starlette:
    return create_app(os.getenv("MCP_CONFIG"))


__all__ = ["CatalogError", "create_app", "create_app_from_env"]
