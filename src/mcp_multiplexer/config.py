from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Compatibility(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tools: Literal["exact"] = "exact"
    schemas: Literal["exact"] = "exact"
    version: Literal["exact", "ignore"] = "exact"


class BaseConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str
    value: str | None = None
    url: str | None = None
    rpc_url: str | None = Field(default=None, alias="rpcUrl")
    service_root: str = Field(default="mcp", alias="serviceRoot")
    username: str | None = None
    password: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    max_concurrent: int = Field(default=4, alias="maxConcurrent")

    @model_validator(mode="after")
    def _validate(self) -> BaseConfig:
        if not self.rpc_url and not self.url:
            raise ValueError(f"base '{self.name}': either url or rpcUrl is required")
        if self.max_concurrent < 1:
            raise ValueError(f"base '{self.name}': maxConcurrent must be >= 1")
        return self

    @property
    def selector(self) -> str:
        return self.value or self.name

    def resolved_rpc_url(self) -> str:
        if self.rpc_url:
            return self.rpc_url
        if not self.url:
            raise ValueError(f"base '{self.name}': either url or rpcUrl is required")
        return f"{self.url.rstrip('/')}/hs/{self.service_root.strip('/')}/rpc"

    def auth(self) -> tuple[str, str] | None:
        if self.username is None and self.password is None:
            return None
        return (self.username or "", self.password or "")


class FanOutConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    enabled: bool = False
    tools: list[str] = Field(default_factory=list)
    max_concurrency: int = Field(default=4, alias="maxConcurrency")
    timeout_ms: int = Field(default=30_000, alias="timeoutMs")

    @model_validator(mode="after")
    def _validate(self) -> FanOutConfig:
        if self.enabled and not self.tools:
            raise ValueError("fanOut.tools must be a non-empty list when fanOut is enabled")
        if self.max_concurrency < 1:
            raise ValueError("fanOut.maxConcurrency must be >= 1")
        if self.timeout_ms < 1:
            raise ValueError("fanOut.timeoutMs must be >= 1")
        return self


class MultiplexerConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str
    description: str | None = None
    group_by: str = Field(default="base", alias="groupBy")
    reference: str | None = None
    compatibility: Compatibility = Field(default_factory=Compatibility)
    on_incompatible: Literal["hide", "fail"] = Field(default="hide", alias="onIncompatible")
    fan_out: FanOutConfig = Field(default_factory=FanOutConfig, alias="fanOut")
    tools_enabled: list[str] = Field(default_factory=list, alias="toolsEnabled")
    bases: list[BaseConfig]

    @model_validator(mode="after")
    def _validate(self) -> MultiplexerConfig:
        if not self.name:
            raise ValueError("name is required")
        if not self.bases:
            raise ValueError("bases must not be empty")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.group_by):
            raise ValueError(f"groupBy must be a valid identifier, got {self.group_by!r}")

        names = [b.name for b in self.bases]
        if len(names) != len(set(names)):
            raise ValueError("base names must be unique")
        selectors = [b.selector for b in self.bases]
        if len(selectors) != len(set(selectors)):
            raise ValueError("base value/name must be unique")
        if "all" in selectors:
            raise ValueError("'all' is reserved for fan-out and cannot be a base value")

        if self.reference is None:
            self.reference = self.bases[0].name
        if self.reference not in names:
            raise ValueError(f"reference '{self.reference}' is not in bases")

        return self

    @property
    def reference_base(self) -> BaseConfig:
        assert self.reference is not None
        return next(b for b in self.bases if b.name == self.reference)

    def base_by_selector(self, selector: str) -> BaseConfig | None:
        for base in self.bases:
            if base.selector == selector:
                return base
        return None

    def base_by_name(self, name: str) -> BaseConfig | None:
        for base in self.bases:
            if base.name == name:
                return base
        return None


def load_config(path: str | Path) -> MultiplexerConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"config file not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return MultiplexerConfig.model_validate(raw)


def config_path_from_env() -> str:
    return os.getenv("MCP_CONFIG", "config/config.json")
