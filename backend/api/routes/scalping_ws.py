"""
WebSocket endpoint for real-time scalping signal feed.

Clients connect to ``/ws/scalping`` and receive all ``scalp_signal`` events
broadcast by the scalping engine (both the Celery runner and the WS stream
engine).  Any other event types (trade, notification, etc.) are also forwarded
so the client can choose to display them; the recommended pattern is to filter
on ``msg.type === "scalp_signal"`` in the frontend.

Auth
----
Same JWT scheme as the primary ``/ws`` endpoint:
  1. ``access_token`` httpOnly cookie set at login.
  2. ``?token=<jwt>`` query parameter (for clients that cannot set cookies).

The connection is rejected with WS close code 4001 before ``accept()`` if the
JWT is missing or invalid.

Usage
-----
    const ws = new WebSocket(`ws://host/ws/scalping?token=${jwt}`);
    ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        if (msg.type === "scalp_signal") { /* handle */ }
    };
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from jose import JWTError, jwt as _jwt
from loguru import logger

from api.websocket import manager
from config import settings as _cfg

router = APIRouter()


@router.websocket("/scalping")
async def scalping_ws_endpoint(
    websocket: WebSocket,
    token: str = Query(default=""),
):
    """
    WebSocket endpoint for the live scalping signal feed.

    Connects the client to the shared broadcast manager so it receives
    ``scalp_signal`` events as soon as they are emitted by either the
    Celery scalping_runner or the ScalpingStreamManager WS engine.
    """
    # ── JWT authentication before accepting the connection ────────────────────
    _raw_token = token or websocket.cookies.get("access_token", "")
    if not _raw_token:
        await websocket.close(code=4001, reason="Unauthorized")
        return
    try:
        _payload = _jwt.decode(_raw_token, _cfg.secret_key, algorithms=["HS256"])
        if not _payload.get("sub"):
            raise JWTError()
    except JWTError:
        await websocket.close(code=4001, reason="Invalid or expired token")
        return

    await manager.connect(websocket)
    logger.info("[ScalpingWS] Client connected.")
    try:
        # Keep the connection alive; the server pushes events via broadcast().
        # Receiving a message from the client is not required but we drain the
        # receive queue so a misbehaving client cannot stall the event loop.
        while True:
            # drain any frame type (text, binary, ping) — WebSocketDisconnect
            # is raised by receive() on clean close, not just receive_text().
            await websocket.receive()
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket)
        logger.info("[ScalpingWS] Client disconnected.")
