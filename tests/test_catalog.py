from __future__ import annotations

from typing import Any

import pytest
from mcp import types

from mcp_multiplexer.catalog import CatalogError, build_catalog
from mcp_multiplexer.config import MultiplexerConfig
from mcp_multiplexer.upstream import UpstreamError


def _tool(name: str = "execute_query", extra_props: dict[str, Any] | None = None) -> dict[str, Any]:
    props = {
        "query": {"type": "string"},
    }
    if extra_props:
        props.update(extra_props)
    return {
        "name": name,
        "description": f"Run {name}",
        "inputSchema": {
            "type": "object",
            "properties": props,
            "required": ["query"],
        },
    }


class FakeClient:
    def __init__(
        self,
        version: str | None = "1.6.3",
        tools: list[dict[str, Any]] | None = None,
        *,
        fail_initialize: bool = False,
        fail_list: bool = False,
        call_text: str = "ok",
    ) -> None:
        self.version = version
        self.tools = tools if tools is not None else [_tool()]
        self.fail_initialize = fail_initialize
        self.fail_list = fail_list
        self.call_text = call_text
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def initialize_version(self) -> str | None:
        if self.fail_initialize:
            raise UpstreamError("offline")
        return self.version

    async def list_tools(self) -> list[dict[str, Any]]:
        if self.fail_list:
            raise UpstreamError("offline")
        return self.tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        self.calls.append((name, arguments))
        return types.CallToolResult(content=[types.TextContent(type="text", text=self.call_text)])


def _config(
    bases: list[dict[str, Any]],
    *,
    fanout: dict[str, Any] | None = None,
    on_incompatible: str = "hide",
    group_by: str = "base",
) -> MultiplexerConfig:
    return MultiplexerConfig.model_validate(
        {
            "name": "data-farm",
            "groupBy": group_by,
            "reference": bases[0]["name"],
            "onIncompatible": on_incompatible,
            "fanOut": fanout or {"enabled": False},
            "bases": bases,
        }
    )


@pytest.mark.asyncio
async def test_catalog_merges_only_compatible_members() -> None:
    config = _config(
        [
            {"name": "rt", "value": "rt", "url": "http://rt"},
            {"name": "ut", "value": "ut", "url": "http://ut"},
            {
                "name": "old",
                "value": "old",
                "url": "http://old",
            },
            {
                "name": "extra",
                "value": "extra",
                "url": "http://extra",
            },
        ]
    )
    clients = {
        "rt": FakeClient(),
        "ut": FakeClient(call_text="ut"),
        "old": FakeClient(version="1.6.2"),
        "extra": FakeClient(tools=[_tool(), _tool("other")]),
    }

    catalog = await build_catalog(config, clients)

    assert [m.status for m in catalog.members] == [
        "reference",
        "compatible",
        "incompatible",
        "incompatible",
    ]
    assert len(catalog.tools) == 1
    schema = catalog.tools[0].inputSchema
    assert schema["properties"]["base"]["enum"] == ["rt", "ut"]
    assert "base" in schema["required"]
    assert catalog.member_by_selector("old") is None
    assert catalog.member_by_selector("extra") is None


@pytest.mark.asyncio
async def test_schema_mismatch_hides_member() -> None:
    config = _config(
        [
            {"name": "rt", "value": "rt", "url": "http://rt"},
            {"name": "ut", "value": "ut", "url": "http://ut"},
        ]
    )
    clients = {
        "rt": FakeClient(),
        "ut": FakeClient(tools=[_tool(extra_props={"limit": {"type": "integer"}})]),
    }
    catalog = await build_catalog(config, clients)
    assert catalog.tools[0].inputSchema["properties"]["base"]["enum"] == ["rt"]
    assert catalog.members[1].status == "incompatible"
    assert "schema differs" in (catalog.members[1].reason or "")


@pytest.mark.asyncio
async def test_reference_unavailable_raises() -> None:
    config = _config([{"name": "rt", "value": "rt", "url": "http://rt"}])
    with pytest.raises(CatalogError):
        await build_catalog(config, {"rt": FakeClient(fail_initialize=True)})


@pytest.mark.asyncio
async def test_group_by_collision_raises() -> None:
    config = _config(
        [{"name": "rt", "value": "rt", "url": "http://rt"}],
        group_by="query",
    )
    with pytest.raises(CatalogError, match="already has an input parameter"):
        await build_catalog(config, {"rt": FakeClient()})


@pytest.mark.asyncio
async def test_fanout_enum_and_call() -> None:
    config = _config(
        [
            {"name": "rt", "value": "rt", "url": "http://rt"},
            {"name": "ut", "value": "ut", "url": "http://ut"},
        ],
        fanout={
            "enabled": True,
            "tools": ["execute_query"],
            "maxConcurrency": 2,
            "timeoutMs": 1000,
        },
    )
    clients = {"rt": FakeClient(call_text="rt"), "ut": FakeClient(call_text="ut")}
    catalog = await build_catalog(config, clients)

    schema = catalog.tools[0].inputSchema
    assert schema["properties"]["base"]["enum"] == ["all", "rt", "ut"]

    from mcp_multiplexer.fanout import fan_out_call

    result = await fan_out_call(catalog, clients, "execute_query", {"query": "SELECT 1"})
    assert result.isError is False
    text = result.content[0].text
    assert '"succeeded": 2' in text
    assert '"rt"' in text and '"ut"' in text
    assert clients["rt"].calls == [("execute_query", {"query": "SELECT 1"})]
    assert clients["ut"].calls == [("execute_query", {"query": "SELECT 1"})]


@pytest.mark.asyncio
async def test_tools_enabled_filters_reference_tools() -> None:
    config = MultiplexerConfig.model_validate(
        {
            "name": "data-farm",
            "reference": "rt",
            "toolsEnabled": ["execute_query"],
            "bases": [
                {
                    "name": "rt",
                    "url": "http://rt",
                }
            ],
        }
    )
    clients = {"rt": FakeClient(tools=[_tool("execute_query"), _tool("other")])}
    catalog = await build_catalog(config, clients)
    assert [tool.name for tool in catalog.tools] == ["execute_query"]
