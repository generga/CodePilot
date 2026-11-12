"""In-memory trace recorder used by the initial MVPs."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass
class TraceEvent:
    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )


class TraceRecorder:
    def __init__(self) -> None:
        self._events: list[TraceEvent] = []

    def record(self, name: str, **payload: Any) -> TraceEvent:
        event = TraceEvent(name=name, payload=payload)
        self._events.append(event)
        return event

    def events(self) -> list[dict[str, Any]]:
        return [asdict(event) for event in self._events]

    def write_jsonl(self, path: str | Path) -> None:
        """Persist the current events as one JSON object per line."""

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as handle:
            for event in self.events():
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def clear(self) -> None:
        self._events.clear()
