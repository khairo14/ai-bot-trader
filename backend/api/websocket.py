"""WebSocket connection manager and endpoint for real-time signal/trade broadcasts."""
import json
from typing import Set
from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger


class ConnectionManager:
    """Manages a set of active WebSocket connections and broadcasts messages."""

    def __init__(self):
        self._connections: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._connections.add(ws)
        logger.info(f"[WS] Client connected. Total: {len(self._connections)}")

    def disconnect(self, ws: WebSocket):
        self._connections.discard(ws)
        logger.info(f"[WS] Client disconnected. Total: {len(self._connections)}")

    async def broadcast(self, event_type: str, data: dict):
        """Broadcast a JSON message to all connected clients."""
        payload = json.dumps({"type": event_type, "data": data})
        dead: Set[WebSocket] = set()
        for ws in list(self._connections):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.add(ws)
        self._connections -= dead

    @property
    def active_count(self) -> int:
        return len(self._connections)


# Singleton shared across the app
manager = ConnectionManager()


async def ws_endpoint(websocket: WebSocket):
    """WebSocket endpoint — mounts at /ws in main.py.

    Requires a valid JWT to prevent unauthenticated access to live
    trade/notification broadcasts.  The token is read from:
      1. The httpOnly 'access_token' cookie (set at login).
      2. A ?token=<jwt> query parameter (for clients that can't set cookies).
    """
    # ── Auth check before accepting the connection ────────────────────────
    from jose import JWTError, jwt as _jwt
    from config import settings as _cfg

    _raw_token = (
        websocket.cookies.get("access_token")
        or websocket.query_params.get("token", "")
    )
    _authed = False
    if _raw_token:
        try:
            _payload = _jwt.decode(_raw_token, _cfg.secret_key, algorithms=["HS256"])
            _authed = bool(_payload.get("sub"))
        except JWTError:
            _authed = False

    if not _authed:
        # Reject the upgrade — no WS connection established.
        await websocket.close(code=4001)
        logger.warning("[WS] Rejected unauthenticated connection")
        return

    await manager.connect(websocket)
    try:
        # Send welcome / heartbeat loop
        await manager.broadcast("connected", {"active_clients": manager.active_count})
        while True:
            # Keep alive; we only push from server side
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.warning(f"[WS] Unexpected error: {e}")
        manager.disconnect(websocket)
