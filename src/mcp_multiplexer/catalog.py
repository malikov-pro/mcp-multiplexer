from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from mcp import types

from .config import BaseConfig, MultiplexerConfig
from .upstream import BaseClient, UpstreamError

logger = logging.getLogger(__name__)

MemberStatus = Literal["reference", "compatible", "incompatible", "unreachable"]


class CatalogError(RuntimeError):
    """The multiplexer cannot build a stable tool contract."""


@dataclass
class MemberState:
    base: BaseConfig
    status: MemberStatus
    version: str | None = None
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    reason: str | None = None

    @property
    def name(self) -> str:
        return self.base.name

    @property
    def selector(self) -> str:
        return self.base.selector


@dataclass
class MultiplexedCatalog:
    config: MultiplexerConfig
    reference: MemberState
    members: list[MemberState]
    tools: list[types.Tool]
    tool_values: dict[str, list[str]]

    def member_by_selector(self, selector: str) -> MemberState | None:
        for member in self.members:
            if member.selector == selector and member.status in ("reference", "compatible"):
                return member
        return None

    def compatible_members(self) -> list[MemberState]:
        return [member for member in self.members if member.status in ("reference", "compatible")]

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok" if self.tools else "degraded",
            "name": self.config.name,
            "groupBy": self.config.group_by,
            "reference": self.reference.name,
            "tools": len(self.tools),
            "members": [
                {
                    "name": member.name,
                    "value": member.selector,
                    "status": member.status,
                    "version": member.version,
                    "reason": member.reason,
                }
                for member in self.members
            ],
            "fanOut": {
                "enabled": self.config.fan_out.enabled,
                "tools": self.config.fan_out.tools,
            },
        }


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _tool_name_set(tools: dict[str, dict[str, Any]]) -> set[str]:
    return set(tools)


def _format_diff(reference: set[str], other: set[str]) -> str:
    missing = sorted(reference - other)
    extra = sorted(other - reference)
    parts: list[str] = []
    if missing:
        parts.append("missing: " + ", ".join(missing[:8]))
    if extra:
        parts.append("extra: " + ", ".join(extra[:8]))
    return "; ".join(parts)


async def _fetch_member(base: BaseConfig, client: BaseClient) -> MemberState:
    try:
        version = await client.initialize_version()
        tools_list = await client.list_tools()
    except UpstreamError as exc:
        return MemberState(base=base, status="unreachable", reason=str(exc))

    tools: dict[str, dict[str, Any]] = {}
    for tool in tools_list:
        name = tool["name"]
        if name in tools:
            return MemberState(
                base=base,
                status="incompatible",
                version=version,
                reason=f"duplicate tool name: {name}",
            )
        tools[name] = tool
    return MemberState(base=base, status="compatible", version=version, tools=tools)


def _compare_member(
    config: MultiplexerConfig,
    reference: MemberState,
    member: MemberState,
) -> MemberState:
    if member.status in ("unreachable", "incompatible"):
        return member

    if config.compatibility.version == "exact":
        if reference.version is None or member.version is None:
            member.status = "incompatible"
            member.reason = "version is not reported by one of the servers"
            return member
        if reference.version != member.version:
            member.status = "incompatible"
            member.reason = f"version {member.version} != reference {reference.version}"
            return member

    ref_names = _tool_name_set(reference.tools)
    member_names = _tool_name_set(member.tools)
    if ref_names != member_names:
        member.status = "incompatible"
        member.reason = "tool set differs: " + _format_diff(ref_names, member_names)
        return member

    for name in sorted(ref_names):
        ref_schema = reference.tools[name].get("inputSchema", {})
        member_schema = member.tools[name].get("inputSchema", {})
        if _canonical(ref_schema) != _canonical(member_schema):
            member.status = "incompatible"
            member.reason = f"schema differs: {name}"
            return member

    member.status = "compatible"
    return member


def _make_tool(
    config: MultiplexerConfig,
    raw_tool: dict[str, Any],
    values: list[str],
    *,
    fanout: bool,
) -> types.Tool:
    schema = copy.deepcopy(raw_tool.get("inputSchema") or {})
    if not isinstance(schema, dict):
        raise CatalogError(f"tool '{raw_tool.get('name')}': inputSchema is not an object")
    properties = schema.get("properties")
    if properties is None:
        properties = {}
        schema["properties"] = properties
    if not isinstance(properties, dict):
        raise CatalogError(f"tool '{raw_tool.get('name')}': properties is not an object")
    if config.group_by in properties:
        raise CatalogError(
            f"tool '{raw_tool.get('name')}' already has an input parameter "
            f"named '{config.group_by}'"
        )

    enum = (["all"] if fanout else []) + values
    properties[config.group_by] = {
        "type": "string",
        "enum": enum,
        "description": (
            "Target member. 'all' runs every compatible member in parallel."
            if fanout
            else "Target member."
        ),
    }
    required = schema.get("required")
    if required is None:
        required = []
        schema["required"] = required
    if not isinstance(required, list):
        raise CatalogError(f"tool '{raw_tool.get('name')}': required is not an array")
    if config.group_by not in required:
        required.append(config.group_by)

    tool = types.Tool.model_validate(raw_tool)
    return tool.model_copy(update={"inputSchema": schema})


async def build_catalog(
    config: MultiplexerConfig,
    clients: dict[str, BaseClient],
) -> MultiplexedCatalog:
    reference_base = config.reference_base
    reference = await _fetch_member(reference_base, clients[reference_base.name])
    if reference.status != "compatible":
        raise CatalogError(
            f"reference base '{reference_base.name}' is unavailable: {reference.reason}"
        )
    reference.status = "reference"
    if not reference.tools:
        raise CatalogError(f"reference base '{reference_base.name}' returned no tools")

    members: list[MemberState] = [reference]
    for base in config.bases:
        if base.name == reference_base.name:
            continue
        member = await _fetch_member(base, clients[base.name])
        member = _compare_member(config, reference, member)
        if member.status != "compatible":
            logger.warning(
                "member hidden: name=%s value=%s status=%s reason=%s",
                member.name,
                member.selector,
                member.status,
                member.reason,
            )
        members.append(member)

    if config.on_incompatible == "fail":
        broken = [m for m in members if m.status not in ("reference", "compatible")]
        if broken:
            details = "; ".join(f"{m.name}: {m.reason}" for m in broken)
            raise CatalogError(f"incompatible members with onIncompatible=fail: {details}")

    compatible = [m for m in members if m.status in ("reference", "compatible")]
    values = [m.selector for m in compatible]

    reference_names = [tool["name"] for tool in reference.tools.values()]
    if config.tools_enabled:
        unknown = sorted(set(config.tools_enabled) - set(reference_names))
        if unknown:
            logger.warning("toolsEnabled contains unknown tools: %s", ", ".join(unknown))
        enabled = set(config.tools_enabled)
        reference_names = [name for name in reference_names if name in enabled]

    if config.fan_out.enabled:
        unknown_fanout = sorted(set(config.fan_out.tools) - set(reference_names))
        if unknown_fanout:
            raise CatalogError(
                "fanOut.tools contains unknown or disabled tools: " + ", ".join(unknown_fanout)
            )

    tools: list[types.Tool] = []
    tool_values: dict[str, list[str]] = {}
    for name in reference_names:
        raw = reference.tools[name]
        fanout = config.fan_out.enabled and name in config.fan_out.tools
        tool = _make_tool(config, raw, values, fanout=fanout)
        tools.append(tool)
        tool_values[name] = list(tool.inputSchema.get("properties", {})[config.group_by]["enum"])

    return MultiplexedCatalog(
        config=config,
        reference=reference,
        members=members,
        tools=tools,
        tool_values=tool_values,
    )
