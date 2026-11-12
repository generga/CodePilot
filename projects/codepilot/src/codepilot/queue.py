"""Task queue adapters used by the optional Redis worker deployment."""

from __future__ import annotations

import threading
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class QueueMessage:
    message_id: str
    task_id: str


class TaskQueue(Protocol):
    def enqueue(self, task_id: str) -> str: ...

    def read(self, *, count: int = 1, block_ms: int = 1_000) -> list[QueueMessage]: ...

    def ack(self, message_id: str) -> None: ...

    def depth(self) -> int: ...

    def check_ready(self) -> bool: ...


class InMemoryTaskQueue:
    """Deterministic queue double for tests and local worker experiments."""

    def __init__(self) -> None:
        self._messages: deque[QueueMessage] = deque()
        self._in_flight: dict[str, QueueMessage] = {}
        self._condition = threading.Condition()

    def enqueue(self, task_id: str) -> str:
        if not task_id.strip():
            raise ValueError("task_id must not be empty")
        message = QueueMessage(uuid.uuid4().hex, task_id)
        with self._condition:
            self._messages.append(message)
            self._condition.notify()
        return message.message_id

    def read(self, *, count: int = 1, block_ms: int = 1_000) -> list[QueueMessage]:
        if count <= 0:
            raise ValueError("queue count must be positive")
        with self._condition:
            if not self._messages and block_ms > 0:
                self._condition.wait(timeout=block_ms / 1_000)
            messages: list[QueueMessage] = []
            while self._messages and len(messages) < count:
                message = self._messages.popleft()
                self._in_flight[message.message_id] = message
                messages.append(message)
            return messages

    def ack(self, message_id: str) -> None:
        with self._condition:
            self._in_flight.pop(message_id, None)

    def depth(self) -> int:
        with self._condition:
            return len(self._messages)

    def check_ready(self) -> bool:
        return True


class RedisTaskQueue:
    """Redis Streams queue with consumer-group delivery and stale-message reclaim."""

    def __init__(
        self,
        redis_url: str,
        *,
        stream: str = "codepilot:tasks",
        group: str = "codepilot-workers",
        consumer: str | None = None,
        client: Any | None = None,
        reclaim_after_ms: int = 60_000,
    ) -> None:
        if not redis_url.strip() and client is None:
            raise ValueError("redis_url must not be empty")
        if not stream.strip() or not group.strip():
            raise ValueError("stream and group must not be empty")
        if reclaim_after_ms <= 0:
            raise ValueError("reclaim_after_ms must be positive")
        if client is None:
            try:
                import redis
            except ImportError as exc:  # pragma: no cover - depends on deployment extras
                raise RuntimeError("Redis mode requires the 'redis' package") from exc
            client = redis.Redis.from_url(redis_url, decode_responses=True)
        self.client = client
        self.stream = stream
        self.group = group
        self.consumer = consumer or f"worker-{uuid.uuid4().hex[:12]}"
        self.reclaim_after_ms = reclaim_after_ms
        self._ensure_group()

    def enqueue(self, task_id: str) -> str:
        if not task_id.strip():
            raise ValueError("task_id must not be empty")
        message_id = self.client.xadd(self.stream, {"task_id": task_id})
        return _decode(message_id)

    def read(self, *, count: int = 1, block_ms: int = 1_000) -> list[QueueMessage]:
        if count <= 0:
            raise ValueError("queue count must be positive")
        reclaimed = self.client.xautoclaim(
            self.stream,
            self.group,
            self.consumer,
            min_idle_time=self.reclaim_after_ms,
            start_id="0-0",
            count=count,
        )
        reclaimed_messages = reclaimed[1] if len(reclaimed) > 1 else []
        if reclaimed_messages:
            return _messages(reclaimed_messages)
        batches = self.client.xreadgroup(
            self.group,
            self.consumer,
            {self.stream: ">"},
            count=count,
            block=block_ms,
        )
        if not batches:
            return []
        return _messages(batches[0][1])

    def ack(self, message_id: str) -> None:
        self.client.xack(self.stream, self.group, message_id)

    def depth(self) -> int:
        return int(self.client.xlen(self.stream))

    def check_ready(self) -> bool:
        return bool(self.client.ping())

    def _ensure_group(self) -> None:
        try:
            self.client.xgroup_create(self.stream, self.group, id="0-0", mkstream=True)
        except Exception as exc:  # redis-py exposes ResponseError only at runtime
            if "BUSYGROUP" not in str(exc):
                raise


def _messages(raw_messages: list[Any]) -> list[QueueMessage]:
    messages: list[QueueMessage] = []
    for message_id, fields in raw_messages:
        task_id = fields.get("task_id", fields.get(b"task_id"))
        if task_id is None:
            continue
        messages.append(QueueMessage(_decode(message_id), _decode(task_id)))
    return messages


def _decode(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)
