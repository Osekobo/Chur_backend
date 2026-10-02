"""Live update WebSocket.

Browsers cannot attach an ``Authorization`` header to a WebSocket handshake, so
the short-lived access token is passed as a query parameter.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.core.security import TokenError, decode_access_token
from app.db.models import User
from app.db.session import AsyncSessionLocal
from app.services.realtime import manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["realtime"])

HEARTBEAT_SECONDS = 25


async def _authorise(token: str | None) -> User | None:
    if not token:
        return None
    try:
        payload = decode_access_token(token)
        user_id = uuid.UUID(payload["sub"])
    except (TokenError, ValueError, KeyError):
        return None

    async with AsyncSessionLocal() as session:
        user: User | None = await session.get(User, user_id)
        return user


@router.websocket("/ws")
async def live_updates(websocket: WebSocket, token: str | None = Query(default=None)) -> None:
    user = await _authorise(token)
    if user is None or not user.is_active:
        await websocket.close(code=4401, reason="Unauthorized")
        return

    await manager.connect(websocket)
    await manager.send_personal(
        websocket, {"resource": "system", "action": "connected", "data": {"user": user.email}}
    )
    try:
        while True:
            # Receives client frames (keep-alive / manual ping) and lets the
            # server notice a closed connection promptly.
            await asyncio.wait_for(websocket.receive_text(), timeout=None)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("WebSocket closed for %s: %s", user.email, exc)
    finally:
        await manager.disconnect(websocket)
