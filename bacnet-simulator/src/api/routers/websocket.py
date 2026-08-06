from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, MutableSequence
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect


router = APIRouter(tags=["websocket"])


TokenResolver = Callable[[str], dict | None]


def get_engine(websocket: WebSocket) -> Any | None:
    return getattr(
        websocket.app.state,
        "engine",
        None,
    )


def get_token_resolver(
    websocket: WebSocket,
) -> TokenResolver | None:
    return getattr(
        websocket.app.state,
        "user_from_token",
        None,
    )


def get_websocket_clients(
    websocket: WebSocket,
) -> MutableSequence[WebSocket] | None:
    return getattr(
        websocket.app.state,
        "ws_clients",
        None,
    )


@router.websocket("/ws")
async def simulation_websocket(
    websocket: WebSocket,
):
    engine = get_engine(websocket)
    resolve_token = get_token_resolver(websocket)
    clients = get_websocket_clients(websocket)

    if (
        engine is None
        or resolve_token is None
        or clients is None
    ):
        await websocket.close(code=1011)
        return

    # Browsers cannot normally provide an Authorization header
    # during the WebSocket handshake, so the Vue client sends
    # the access token through the query string.
    token = websocket.query_params.get(
        "token",
        "",
    )

    user = await asyncio.to_thread(
        resolve_token,
        token,
    )

    if user is None:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    clients.append(websocket)

    try:
        # Send the current simulator state immediately instead
        # of waiting for the next simulation tick.
        await websocket.send_text(
            json.dumps(
                engine.get_state()
            )
        )

        while True:
            # The background tick loop pushes state updates.
            # Messages from the browser serve as keep-alives.
            await websocket.receive_text()

    except WebSocketDisconnect:
        pass

    except Exception:
        # The connection may disappear without producing a
        # WebSocketDisconnect, for example during browser refresh.
        pass

    finally:
        if websocket in clients:
            clients.remove(websocket)