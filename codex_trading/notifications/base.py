from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol


@dataclass(frozen=True)
class NotificationMessage:
    kind: str
    title: str
    body: str
    channel_target: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class NotificationProvider(Protocol):
    def send(self, message: NotificationMessage) -> Dict[str, Any]:
        ...


class NotificationDispatcher(Protocol):
    def dispatch(self, message: NotificationMessage) -> Future:
        ...

    def flush(self) -> None:
        ...

