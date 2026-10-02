"""In-process WebSocket connection manager used to push live updates.

The manager keeps a set of connected clients and broadcasts JSON events after
any mutation. For a multi-worker deployment swap the queue for Redis pub/sub;
the route handlers do not need to change.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    async def broadcast(self, event: dict[str, Any]) -> None:
        """Send an event to every connected client, dropping dead sockets."""
        async with self._lock:
            targets: Iterable[WebSocket] = list(self._connections)

        dead: list[WebSocket] = []
        for connection in targets:
            try:
                await connection.send_json(event)
            except Exception:
                dead.append(connection)
        if dead:
            async with self._lock:
                for connection in dead:
                    self._connections.discard(connection)

    async def send_personal(self, websocket: WebSocket, event: dict[str, Any]) -> None:
        await websocket.send_json(event)


manager = ConnectionManager()


def change_event(
    resource: str, action: str, data: Any | None = None, entity_id: str | None = None
) -> dict[str, Any]:
    """Build a consistent change-notification payload."""
    payload: dict[str, Any] = {"resource": resource, "action": action}
    if entity_id is not None:
        payload["id"] = entity_id
    if data is not None:
        payload["data"] = data
    return payload
