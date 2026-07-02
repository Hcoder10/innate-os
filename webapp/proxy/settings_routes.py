"""Settings read endpoint + write WebSocket channel.

Thin HTTP/WS adapters over settings_store, served by https_server.py. Split out
of https_server.py.
"""

import asyncio
import json

from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response


def settings_get_response() -> Response:
    """GET /settings -> the live override values from config/settings.yaml. The
    webapp owns the catalog (knobs/defaults/docs); this only reports what's set."""
    import settings_store

    payload = {"overrides": settings_store.read_overrides(), "exists": settings_store.settings_path().is_file()}
    body = json.dumps(payload).encode()
    return Response(
        200,
        "OK",
        Headers({"Content-Type": "application/json", "Content-Length": str(len(body)), "Cache-Control": "no-cache"}),
        body,
    )


async def settings_ws(connection):
    """Settings write channel: receive {sets, clears} messages, apply each to
    config/settings.yaml, and ack. One persistent WS per open Settings page."""
    import settings_store

    try:
        async for raw in connection:
            try:
                req = json.loads(raw)
                sets = req.get("sets", []) or []
                clears = req.get("clears", []) or []
            except (ValueError, AttributeError, TypeError):
                await connection.send(json.dumps({"ok": False, "message": "malformed request"}))
                continue
            try:
                ok, msg = await asyncio.to_thread(settings_store.apply_changes, sets, clears)
            except Exception as exc:  # noqa: BLE001 — malformed payload, disk error, etc.
                ok, msg = False, f"settings update failed: {exc}"
            await connection.send(json.dumps({"ok": ok, "message": msg}))
    except ConnectionClosed:
        pass  # page closed — normal
