"""Environment-backed settings with no external runtime dependency."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_PROVIDER_DEFAULTS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "model_name": "deepseek-chat",
    },
}


def _read_dotenv(path: str | Path) -> dict[str, str]:
    """Read the small KEY=VALUE subset needed by local development."""

    source = Path(path)
    if not source.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in source.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


@dataclass(frozen=True)
class Settings:
    model_provider: str = "openai_compatible"
    model_base_url: str = ""
    model_api_key: str = ""
    model_name: str = ""
    trace_enabled: bool = True
    model_timeout_seconds: float = 60.0

    @classmethod
    def from_env(cls, dotenv_path: str | Path | None = None) -> Settings:
        dotenv = _read_dotenv(dotenv_path or (Path.cwd() / ".env"))

        def get(name: str, default: str = "") -> str:
            return os.environ[name] if name in os.environ else dotenv.get(name, default)

        provider = get("MODEL_PROVIDER", "openai_compatible").strip().lower()
        defaults = _PROVIDER_DEFAULTS.get(provider, {})
        raw_trace = get("TRACE_ENABLED", "true").strip().lower()
        raw_timeout = get("MODEL_TIMEOUT_SECONDS", "60").strip()
        try:
            timeout = float(raw_timeout)
        except ValueError as exc:
            raise ValueError("MODEL_TIMEOUT_SECONDS must be a positive number") from exc
        if timeout <= 0:
            raise ValueError("MODEL_TIMEOUT_SECONDS must be a positive number")
        return cls(
            model_provider=provider,
            model_base_url=get("MODEL_BASE_URL").strip()
            or str(defaults.get("base_url", "")),
            model_api_key=get("MODEL_API_KEY").strip(),
            model_name=get("MODEL_NAME").strip()
            or str(defaults.get("model_name", "")),
            model_timeout_seconds=timeout,
            trace_enabled=raw_trace not in {"0", "false", "no", "off"},
        )
