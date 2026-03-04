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
    """WebSocket endpoint — mounts at /ws in main.py."""
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
