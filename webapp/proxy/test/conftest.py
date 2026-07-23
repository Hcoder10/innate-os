# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Test harness for the aiohttp webapp front door (proxy/https_server.py).

Plugin-free on purpose: CI runs pytest with PYTEST_DISABLE_PLUGIN_AUTOLOAD=1, so
there is no pytest-asyncio. `sync` runs an async test body via asyncio.run;
`serve()` boots the real app on an ephemeral port with chosen module globals
overridden; `fake_ws_upstream()` stands in for rosbridge / the world server.
"""

import contextlib
import functools
import sys
from pathlib import Path

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

# proxy/ on the path so `import https_server` / `media_routes` / `settings_store` work.
_PROXY = Path(__file__).resolve().parent.parent
if str(_PROXY) not in sys.path:
    sys.path.insert(0, str(_PROXY))

# https_server casts sys.argv[1] to the HTTPS port at import time; under pytest
# that would be a test path. Import it with a clean argv so the int() cast holds.
_saved_argv = sys.argv
sys.argv = [sys.argv[0]]
try:
    import https_server as H  # noqa: E402
finally:
    sys.argv = _saved_argv

import asyncio  # noqa: E402


def sync(fn):
    """Run an async test body under asyncio.run (no pytest-asyncio available).

    functools.wraps keeps the async signature visible, so pytest still injects
    fixtures (tmp_path, monkeypatch) into the wrapper and we forward them on."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


def make_app_root(tmp_path: Path) -> Path:
    """A minimal webapp ROOT: shell + a JS asset + a base config.json."""
    root = tmp_path / "webapp"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>SHELL</title>")
    (root / "assets" / "app.js").write_text("console.log('app')\n")
    (root / "config.json").write_text('{"base": true}')
    return root


@contextlib.asynccontextmanager
async def fake_ws_upstream(prefix: str = "up:"):
    """A WS echo server (text answered with `prefix`+data, binary echoed) that
    stands in for an upstream rosbridge / world-state server. Yields its URL."""

    async def handler(request):
        ws = web.WebSocketResponse(max_msg_size=0)
        await ws.prepare(request)
        async for m in ws:
            if m.type == aiohttp.WSMsgType.TEXT:
                await ws.send_str(prefix + m.data)
            elif m.type == aiohttp.WSMsgType.BINARY:
                await ws.send_bytes(m.data)
        return ws

    app = web.Application()
    app.router.add_get("/", handler)
    srv = TestServer(app)
    await srv.start_server()
    try:
        yield f"http://127.0.0.1:{srv.port}/"
    finally:
        await srv.close()


@contextlib.asynccontextmanager
async def serve(**overrides):
    """Boot the real app with the given https_server globals overridden (e.g.
    ROOT=..., ROSBRIDGE_URL=..., WEBAPP_SIM_CONTROLS=True). Yields
    (aiohttp.ClientSession, base_url) and restores the globals on exit."""
    saved = {k: getattr(H, k) for k in overrides}
    for k, v in overrides.items():
        setattr(H, k, v)
    srv = TestServer(H.build_app())
    await srv.start_server()
    session = aiohttp.ClientSession()
    try:
        yield session, f"http://127.0.0.1:{srv.port}"
    finally:
        await session.close()
        await srv.close()
        for k, v in saved.items():
            setattr(H, k, v)
