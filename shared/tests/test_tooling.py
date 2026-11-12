import pytest
from agent_shared import ToolRegistry, ToolResult, ToolSpec


def test_registry_exposes_schema_and_executes_handler():
    registry = ToolRegistry(
        [
            ToolSpec(
                name="echo",
                description="Echo a value",
                parameters={"type": "object", "properties": {"value": {"type": "string"}}},
                handler=lambda arguments: {"value": arguments["value"]},
            )
        ]
    )

    result = registry.execute("echo", {"value": "ok"})

    assert result == ToolResult.success({"value": "ok"})
    assert registry.schemas()[0]["function"]["name"] == "echo"


def test_registry_normalizes_unknown_and_handler_errors():
    registry = ToolRegistry(
        [
            ToolSpec("broken", "Always fails", {"type": "object"}, lambda _: 42),
        ]
    )

    unknown = registry.execute("missing", {})
    broken = registry.execute("broken", {})

    assert unknown.ok is False
    assert "unknown tool" in (unknown.error or "")
    assert broken.ok is False
    assert "unsupported result" in (broken.error or "")


def test_registry_rejects_duplicate_names():
    spec = ToolSpec("echo", "Echo", {"type": "object"}, lambda _: ToolResult.success())
    registry = ToolRegistry([spec])

    with pytest.raises(ValueError, match="already registered"):
        registry.register(spec)
