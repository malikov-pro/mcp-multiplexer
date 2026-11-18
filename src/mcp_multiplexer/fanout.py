from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from mcp import types

from .catalog import MultiplexedCatalog
from .upstream import BaseClient

logger = logging.getLogger(__name__)


async def fan_out_call(
    catalog: MultiplexedCatalog,
    clients: dict[str, BaseClient],
    tool_name: str,
    arguments: dict[str, Any],
) -> types.CallToolResult:
    """Run one read-only tool on every compatible member in parallel."""
    config = catalog.config
    if not config.fan_out.enabled or tool_name not in config.fan_out.tools:
        raise ValueError(f"fan-out is not enabled for tool '{tool_name}'")

    targets = catalog.compatible_members()
    semaphore = asyncio.Semaphore(max(1, config.fan_out.max_concurrency))
    timeout = config.fan_out.timeout_ms / 1000.0
    started = time.monotonic()

    async def call_one(member) -> dict[str, Any]:
        client = clients[member.name]
        async with semaphore:
            try:
                result = await asyncio.wait_for(
                    client.call_tool(tool_name, arguments),
                    timeout=timeout,
                )
                return {
                    "target": member.selector,
                    "server": member.name,
                    "ok": True,
                    "result": result.model_dump(mode="json", exclude_none=True),
                }
            except TimeoutError:
                return {
                    "target": member.selector,
                    "server": member.name,
                    "ok": False,
                    "error": f"timeout after {config.fan_out.timeout_ms} ms",
                }
            except Exception as exc:  # noqa: BLE001 - per-target error is data
                return {
                    "target": member.selector,
                    "server": member.name,
                    "ok": False,
                    "error": str(exc),
                }

    results = await asyncio.gather(*(call_one(member) for member in targets))
    succeeded = sum(1 for item in results if item["ok"])
    payload = {
        "tool": tool_name,
        "summary": {
            "requested": len(results),
            "succeeded": succeeded,
            "failed": len(results) - succeeded,
            "elapsedMs": round((time.monotonic() - started) * 1000),
        },
        "results": results,
    }
    logger.info(
        "fan-out tool=%s targets=%s ok=%s failed=%s",
        tool_name,
        len(results),
        succeeded,
        len(results) - succeeded,
    )
    return types.CallToolResult(
        content=[
            types.TextContent(
                type="text",
                text=json.dumps(payload, ensure_ascii=False, indent=2),
            )
        ]
    )
