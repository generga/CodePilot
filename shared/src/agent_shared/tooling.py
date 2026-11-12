"""Reusable tool registration and execution contracts.

Business projects provide handlers; this module owns only the protocol that
lets an agent expose tools to a model and execute them consistently.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

ToolHandler = Callable[[dict[str, Any]], "ToolResult | Mapping[str, Any]"]


@dataclass(frozen=True)
class ToolResult:
    """A normalized result returned by a registered tool."""

    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    pause: bool = False

    @classmethod
    def success(cls, data: Mapping[str, Any] | None = None, *, pause: bool = False) -> ToolResult:
        return cls(ok=True, data=dict(data or {}), pause=pause)

    @classmethod
    def failure(cls, error: str) -> ToolResult:
        return cls(ok=False, error=error)

    def as_message(self) -> dict[str, Any]:
        if self.ok:
            return dict(self.data)
        return {"error": self.error or "tool failed"}


@dataclass(frozen=True)
class ToolSpec:
    """Model-facing metadata plus a callable implementation."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """Register, expose, and safely execute a project's tools."""

    def __init__(self, specs: list[ToolSpec] | None = None) -> None:
        self._specs: dict[str, ToolSpec] = {}
        for spec in specs or []:
            self.register(spec)

    def register(self, spec: ToolSpec) -> None:
        if not spec.name.strip():
            raise ValueError("tool name must not be empty")
        if spec.name in self._specs:
            raise ValueError(f"tool already registered: {spec.name}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise KeyError(f"unknown tool: {name}") from exc

    def schemas(self) -> list[dict[str, Any]]:
        return [spec.schema() for spec in self._specs.values()]

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if not isinstance(arguments, dict):
            return ToolResult.failure("tool arguments must be an object")
        try:
            result = self.get(name).handler(arguments)
        except (AttributeError, IndexError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            return ToolResult.failure(str(exc))
        if isinstance(result, ToolResult):
            return result
        if isinstance(result, Mapping):
            return ToolResult.success(result)
        return ToolResult.failure("tool handler returned an unsupported result")
