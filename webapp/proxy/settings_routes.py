"""Settings read endpoint + write WebSocket channel.

Thin HTTP/WS adapters over settings_store, served by https_server.py. Split out
of https_server.py.
"""

import asyncio
import json

from aiohttp import WSMsgType, web


def settings_get_response() -> web.Response:
    """GET /settings -> the live override values from config/settings.yaml. The
    webapp owns the catalog (knobs/defaults/docs); this only reports what's set."""
    import settings_store

    payload = {"overrides": settings_store.read_overrides(), "exists": settings_store.settings_path().is_file()}
    return web.json_response(payload, headers={"Cache-Control": "no-cache"})


async def settings_ws(ws: web.WebSocketResponse) -> None:
    """Settings write channel: receive {sets, clears} messages, apply each to
    config/settings.yaml, and ack. One persistent WS per open Settings page.

    The caller has already prepared the socket; iterating it ends cleanly when
    the page closes."""
    import settings_store

    async for msg in ws:
        if msg.type != WSMsgType.TEXT:
            continue
        try:
            req = json.loads(msg.data)
            sets = req.get("sets", []) or []
            clears = req.get("clears", []) or []
        except (ValueError, AttributeError, TypeError):
            await ws.send_str(json.dumps({"ok": False, "message": "malformed request"}))
            continue
        try:
            ok, message = await asyncio.to_thread(settings_store.apply_changes, sets, clears)
        except Exception as exc:  # noqa: BLE001 — malformed payload, disk error, etc.
            ok, message = False, f"settings update failed: {exc}"
        await ws.send_str(json.dumps({"ok": ok, "message": message}))
