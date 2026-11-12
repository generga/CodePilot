"""Small shared contracts for the portfolio projects."""

from .config import Settings
from .llm import (
    ModelClient,
    ModelRequest,
    ModelResponse,
    OpenAICompatibleClient,
    ScriptedModel,
    ToolCall,
)
from .models import RunStatus, ToolCallRecord
from .tooling import ToolRegistry, ToolResult, ToolSpec
from .trace import TraceRecorder

__all__ = [
    "ModelClient",
    "ModelRequest",
    "ModelResponse",
    "OpenAICompatibleClient",
    "RunStatus",
    "ScriptedModel",
    "Settings",
    "ToolCall",
    "ToolCallRecord",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "TraceRecorder",
]
