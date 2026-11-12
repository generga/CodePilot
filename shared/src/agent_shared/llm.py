"""Small model adapter contract shared by the portfolio projects.

The initial implementation speaks the common OpenAI-compatible chat format
using only the Python standard library. A scripted client keeps tests and
local workflow development independent from network access and API keys.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelRequest:
    messages: tuple[dict[str, Any], ...]
    tools: tuple[dict[str, Any], ...] = ()
    temperature: float = 0.0
    response_format: dict[str, Any] | None = None


@dataclass(frozen=True)
class ModelResponse:
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


class ModelClient(Protocol):
    def complete(self, request: ModelRequest) -> ModelResponse:
        """Return one assistant turn for a structured chat request."""


class ScriptedModel:
    """Deterministic model substitute for tests and offline development."""

    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if not self._responses:
            raise RuntimeError("scripted model has no response left")
        return self._responses.pop(0)


class OpenAICompatibleClient:
    """Minimal synchronous client for chat-completions-compatible endpoints."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout_seconds: float = 60.0,
    ) -> None:
        if not base_url.strip():
            raise ValueError("base_url must not be empty")
        if not model.strip():
            raise ValueError("model must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._endpoint = base_url.rstrip("/") + "/chat/completions"
        self._model = model
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds

    def complete(self, request: ModelRequest) -> ModelResponse:
        payload = {
            "model": self._model,
            "messages": list(request.messages),
            "temperature": request.temperature,
        }
        if request.tools:
            payload["tools"] = list(request.tools)
        if request.response_format:
            payload["response_format"] = request.response_format
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        http_request = Request(self._endpoint, data=body, headers=headers, method="POST")
        try:
            with urlopen(http_request, timeout=self._timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"model endpoint returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"model endpoint is unavailable: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("model endpoint returned invalid JSON") from exc
        return self._decode_response(data)

    @staticmethod
    def _decode_response(data: dict[str, Any]) -> ModelResponse:
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("model response has no assistant message") from exc

        tool_calls: list[ToolCall] = []
        for raw_call in message.get("tool_calls", []):
            function = raw_call.get("function", {})
            raw_arguments = function.get("arguments", {})
            if isinstance(raw_arguments, str):
                try:
                    arguments = json.loads(raw_arguments)
                except json.JSONDecodeError as exc:
                    raise RuntimeError("model returned invalid tool arguments JSON") from exc
            else:
                arguments = raw_arguments
            if not isinstance(arguments, dict):
                raise TypeError("tool arguments must be a JSON object")
            tool_calls.append(
                ToolCall(
                    id=str(raw_call.get("id", f"call-{len(tool_calls) + 1}")),
                    name=str(function.get("name", "")),
                    arguments=arguments,
                )
            )

        usage = data.get("usage", {})
        return ModelResponse(
            content=str(message.get("content") or ""),
            tool_calls=tuple(tool_calls),
            input_tokens=int(usage.get("prompt_tokens", 0) or 0),
            output_tokens=int(usage.get("completion_tokens", 0) or 0),
            raw=data,
        )
