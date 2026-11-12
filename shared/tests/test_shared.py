import pytest
from agent_shared import (
    ModelRequest,
    ModelResponse,
    OpenAICompatibleClient,
    ScriptedModel,
    Settings,
    ToolCall,
    TraceRecorder,
)


def test_settings_defaults_are_safe(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)
    monkeypatch.delenv("MODEL_BASE_URL", raising=False)
    monkeypatch.delenv("MODEL_NAME", raising=False)
    monkeypatch.delenv("MODEL_TIMEOUT_SECONDS", raising=False)
    settings = Settings.from_env()
    assert settings.model_provider == "openai_compatible"
    assert settings.model_api_key == ""
    assert settings.trace_enabled is True
    assert settings.model_timeout_seconds == 60


def test_settings_resolve_deepseek_defaults(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MODEL_PROVIDER", "DeepSeek")
    monkeypatch.delenv("MODEL_BASE_URL", raising=False)
    monkeypatch.delenv("MODEL_NAME", raising=False)
    monkeypatch.setenv("MODEL_API_KEY", "test-key")

    settings = Settings.from_env()

    assert settings.model_provider == "deepseek"
    assert settings.model_base_url == "https://api.deepseek.com"
    assert settings.model_name == "deepseek-chat"


def test_settings_reject_invalid_model_timeout(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MODEL_TIMEOUT_SECONDS", "0")

    with pytest.raises(ValueError, match="positive number"):
        Settings.from_env()


def test_settings_reads_local_dotenv_without_overriding_process_env(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "MODEL_PROVIDER=deepseek\nMODEL_API_KEY=file-key\nMODEL_NAME=deepseek-chat\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_NAME", raising=False)

    settings = Settings.from_env(dotenv)

    assert settings.model_provider == "deepseek"
    assert settings.model_api_key == "file-key"
    assert settings.model_name == "deepseek-chat"

    monkeypatch.setenv("MODEL_API_KEY", "process-key")
    assert Settings.from_env(dotenv).model_api_key == "process-key"


def test_trace_recorder_keeps_structured_events():
    recorder = TraceRecorder()
    recorder.record("tool.completed", tool="search_code", result_count=2)
    events = recorder.events()
    assert events[0]["name"] == "tool.completed"
    assert events[0]["payload"]["result_count"] == 2


def test_trace_recorder_writes_jsonl(tmp_path):
    recorder = TraceRecorder()
    recorder.record("run.started", project="codepilot")
    recorder.record("run.completed", phase="completed")

    path = tmp_path / "traces" / "run.jsonl"
    recorder.write_jsonl(path)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert '"name": "run.started"' in lines[0]
    assert '"phase": "completed"' in lines[1]


def test_scripted_model_records_requests_and_returns_response():
    response = ModelResponse(
        content="search first",
        tool_calls=(ToolCall("call-1", "search_code", {"query": "email"}),),
    )
    model = ScriptedModel([response])
    request = ModelRequest(messages=({"role": "user", "content": "fix email bug"},))

    assert model.complete(request) == response
    assert model.requests == [request]


def test_openai_compatible_response_decoding():
    response = OpenAICompatibleClient._decode_response(
        {
            "choices": [
                {
                    "message": {
                        "content": "done",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path":"user.py"}',
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
    )

    assert response.content == "done"
    assert response.tool_calls[0].arguments == {"path": "user.py"}
    assert response.input_tokens == 10


def test_deepseek_request_uses_compatible_endpoint_and_auth(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"ok"}}]}'

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("agent_shared.llm.urlopen", fake_urlopen)
    client = OpenAICompatibleClient(
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        api_key="sk-test-only",
        timeout_seconds=12,
    )

    response = client.complete(
        ModelRequest(
            messages=({"role": "user", "content": "ping"},),
            response_format={"type": "json_object"},
        )
    )

    assert response.content == "ok"
    assert captured["request"].full_url == "https://api.deepseek.com/chat/completions"
    assert captured["request"].get_header("Authorization") == "Bearer sk-test-only"
    assert captured["timeout"] == 12
    assert b'"model": "deepseek-chat"' in captured["request"].data
    assert b'"response_format": {"type": "json_object"}' in captured["request"].data


def test_openai_compatible_client_validates_configuration():
    with pytest.raises(ValueError):
        OpenAICompatibleClient(base_url="", model="demo")
