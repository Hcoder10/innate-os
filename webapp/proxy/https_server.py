#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""HTTPS front door for the Innate webapp.

Serves the static app AND proxies /ws to the local rosbridge (rws) over one
TLS port, so a single self-signed certificate acceptance gives the browser a
secure origin for everything — which is what unlocks WebSerial (leader-arm
teleop) without serving the app from the operator's laptop.

The read-only episode/run media endpoints live in media_routes.py and the
settings read/write endpoints in settings_routes.py; this module is the server
itself (TLS, static, routing, the /ws relay), built on aiohttp. It serves the
same app on both a TLS port (HTTPS) and a cleartext port (HTTP). The
secure-origin features (WebSerial leader-arm) need HTTPS; the arm panel offers a
one-click switch rather than an automatic bounce. A self-signed certificate is
generated on first run (10 years) under ~/.innate-webapp-tls/ via openssl.

Static files are served with aiohttp's FileResponse: a single stat() yields the
mtime+size ETag, a matching If-None-Match returns a bodyless 304 before the file
is read, Range is honoured, and compressible types are gzipped. Everything is
no-cache — the browser revalidates every load, but revalidation is a cheap
conditional request. The sim 3D assets ship mtime-normalised in a reproducible
tarball; the launcher stamps a real mtime on extraction (see ensure_sim_assets)
so the validator does not degrade to size-only.

Run:        python3 proxy/https_server.py        # https://<robot>:443 + http://<robot>:80
Persist:    launched on boot in the `console-webapp` tmux window
            (innate-os/scripts/launch_ros_in_tmux.sh).
"""

import asyncio
import json
import logging
import mimetypes
import os
import shutil
import ssl
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs

import aiohttp
from aiohttp import web

from media_routes import (
    episode_response,
    joints_response,
    map_preview_response,
    profile_response,
    run_info_response,
    run_log_response,
    thumb_response,
)
from settings_routes import settings_get_response, settings_ws

HTTPS_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 443
# Cleartext HTTP listener serving the SAME app as the TLS front door, so the site
# is reachable over http://<robot> too. The secure-origin features (WebSerial
# leader-arm) need HTTPS; the arm panel offers a one-click switch rather than an
# automatic bounce — the self-signed cert means any upgrade costs a warning
# click-through anyway, and the browser caches that acceptance. Native clients
# (the mobile app) that can't accept the self-signed cert also reach /episode*
# here. 0 disables it.
# NOTE: 80/443 are privileged — bind needs root or cap_net_bind_service.
HTTP_PORT = int(os.environ.get("INNATE_HTTP_PORT", "80"))

ROOT = Path(__file__).resolve().parent.parent
# The sim launch sets this so the webapp's sim-only debug controls (Reset
# Position + FPS/queue) surface without editing the committed (robot-default)
# config.json. Overlaid onto /config.json at request time.
WEBAPP_SIM_CONTROLS = os.environ.get("WEBAPP_SIM_CONTROLS", "").strip().lower() in ("1", "true", "yes")
CERT_DIR = Path.home() / ".innate-webapp-tls"
ROSBRIDGE_URL = "ws://127.0.0.1:9090"

# /worldstate -> the sim world server's observer stream: host of
# VIRTUAL_MARS_REMOTE (else in-container), always port 8800 (its default).
_WORLD_HOST = os.environ.get("VIRTUAL_MARS_REMOTE", "").strip().partition(":")[0] or "127.0.0.1"
WORLD_STATE_URL = f"ws://{_WORLD_HOST}:8800"


def _quiet_benign_disconnects() -> None:
    """Drop the traceback aiohttp logs when a client vanishes mid-request —
    browser reloads, cert-warning preconnects, and port scanners routinely reset
    the socket, surfacing as ConnectionResetError under 'aiohttp.server'. Filter
    just those out while keeping every other error."""

    class _DropReset(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            exc = record.exc_info[1] if record.exc_info else None
            return not isinstance(exc, (ConnectionResetError, asyncio.CancelledError))

    logging.getLogger("aiohttp.server").addFilter(_DropReset())


def ensure_cert() -> tuple[Path, Path]:
    cert, key = CERT_DIR / "cert.pem", CERT_DIR / "key.pem"
    if cert.exists() and key.exists():
        return cert, key
    CERT_DIR.mkdir(mode=0o700, exist_ok=True)
    hostname = subprocess.run(["hostname"], capture_output=True, text=True).stdout.strip() or "robot"
    ips = subprocess.run(["hostname", "-I"], capture_output=True, text=True).stdout.split()
    sans = [f"DNS:{hostname}.local", f"DNS:{hostname}", "DNS:localhost"] + [f"IP:{ip}" for ip in ips if ":" not in ip]
    openssl_cmd = [
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "ec",
        "-pkeyopt",
        "ec_paramgen_curve:prime256v1",
        "-keyout",
        str(key),
        "-out",
        str(cert),
        "-days",
        "3650",
        "-nodes",
        "-subj",
        f"/CN={hostname}.local",
        "-addext",
        f"subjectAltName={','.join(sans)}",
    ]
    try:
        subprocess.run(openssl_cmd, check=True, capture_output=True)
    except FileNotFoundError:
        # Fail loudly — otherwise the unit just respawns and a "site won't load"
        # symptom hides the real cause (no openssl on PATH).
        print("FATAL: openssl not found — cannot generate the HTTPS certificate.", file=sys.stderr, flush=True)
        raise
    except subprocess.CalledProcessError as exc:
        # capture_output swallows openssl's stderr; print it so each restart says why.
        print(
            f"FATAL: openssl failed to generate the HTTPS certificate:\n{exc.stderr.decode(errors='replace')}",
            file=sys.stderr,
            flush=True,
        )
        raise
    key.chmod(0o600)
    print(f"generated self-signed cert for {', '.join(sans)}")
    return cert, key


CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".avif": "image/avif",
    ".png": "image/png",
    ".md": "text/plain; charset=utf-8",
    ".tgz": "application/gzip",
    ".mp4": "video/mp4",
    # Sim viewer assets.
    ".glb": "model/gltf-binary",
    ".obj": "text/plain; charset=utf-8",
    ".urdf": "application/xml",
    ".stl": "application/octet-stream",
}

# Simulation viewer (sim/viewer): the SimSession bundle plus the 3D assets it
# fetches at their canonical absolute paths. Only served when the directories
# exist (i.e. in the sim container / a dev checkout, never on the robot).
SIM_VIEWER_ROOT = ROOT.parent / "sim" / "viewer"
SIM_VIEWER_ROUTES = {
    "/sim-viewer/": SIM_VIEWER_ROOT / "dist-lib",
    "/models/": SIM_VIEWER_ROOT / "public" / "models",
    "/robot/": SIM_VIEWER_ROOT / "public" / "robot",
    # Collision hulls for the SimSession's "collisions" debug overlay.
    "/physics/": SIM_VIEWER_ROOT / "public" / "physics",
}

# Types worth gzipping on the fly; binary assets (glb, mp4, png, avif) are
# already compressed and are left to stream (sendfile on cleartext). Matched with
# str.startswith so charset suffixes ("text/html; charset=utf-8") still count.
_COMPRESSIBLE = (
    "text/",
    "application/json",
    "application/x-ndjson",
    "application/xml",
    "image/svg+xml",
)


def _content_type(path: Path) -> str:
    return CONTENT_TYPES.get(path.suffix) or mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def _serve_static(request: web.Request, path: Path) -> web.StreamResponse:
    """Serve a file no-cache. Binary/large assets go through FileResponse, which
    streams them (sendfile on cleartext) and adds the mtime+size ETag, the
    bodyless 304, and Range natively — and which can't be gzipped anyway
    (glb/png/mp4 are already compressed; FileResponse keeps the uncompressed
    Content-Length, so on-the-fly gzip would corrupt framing).

    Compressible text (js/css/html/json/svg — small) is served from memory as a
    plain Response so the compress middleware *can* gzip it, with the same
    mtime+size ETag FileResponse would have used, so revalidation still 304s."""
    ct = _content_type(path)
    if not ct.startswith(_COMPRESSIBLE):
        return web.FileResponse(path, headers={"Content-Type": ct, "Cache-Control": "no-cache"})
    st = path.stat()
    etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if request.headers.get("If-None-Match") == etag:
        return web.Response(status=304, headers=headers)
    return web.Response(body=path.read_bytes(), headers={**headers, "Content-Type": ct})


@web.middleware
async def compress_middleware(request: web.Request, handler) -> web.StreamResponse:
    """gzip compressible in-memory 200s when the client accepts it. FileResponse
    is excluded on purpose — it keeps a fixed uncompressed Content-Length, so
    compressing it corrupts the response framing. 304/206/101 fall through."""
    resp = await handler(request)
    if (
        not isinstance(resp, web.FileResponse)
        and resp.status == 200
        and "gzip" in request.headers.get("Accept-Encoding", "")
        and (resp.headers.get("Content-Type") or "").startswith(_COMPRESSIBLE)
    ):
        resp.enable_compression()
    return resp


async def static_handler(request: web.Request) -> web.StreamResponse:
    clean = request.path
    target = (ROOT / clean.lstrip("/")).resolve()
    if target.is_dir():
        target = target / "index.html"
    body_path = target if target.is_file() else None
    # SPA fallback: an extensionless route that maps to no file (e.g. /profiling,
    # /settings, or a deep link/refresh on any client-side route) is served the
    # app shell so the router can render it. Asset requests carry a suffix
    # (.js/.css/...), so a genuinely missing asset still 404s below. The
    # in-root guard keeps a traversal like /../secrets a 404, not the shell.
    if body_path is None and target.is_relative_to(ROOT) and "." not in clean.rsplit("/", 1)[-1]:
        body_path = ROOT / "index.html"
    # Refuse anything that escapes the app root (or the TLS keys, defensively).
    if body_path is None or not body_path.is_relative_to(ROOT) or body_path.suffix == ".pem":
        raise web.HTTPNotFound(text="not found")
    return _serve_static(request, body_path)


async def sim_viewer_handler(request: web.Request) -> web.StreamResponse:
    clean = request.path
    for prefix, base in SIM_VIEWER_ROUTES.items():
        if not clean.startswith(prefix):
            continue
        target = (base / clean[len(prefix) :]).resolve()
        if not target.is_file() or not target.is_relative_to(base.resolve()):
            raise web.HTTPNotFound(text="not found")
        return _serve_static(request, target)
    raise web.HTTPNotFound(text="not found")


async def config_handler(request: web.Request) -> web.Response:
    """Serve config.json with env-driven feature flags overlaid, so a deployment
    can flip flags without editing the committed file (the sim sets
    WEBAPP_SIM_CONTROLS=1)."""
    try:
        cfg = json.loads((ROOT / "config.json").read_text())
    except Exception:
        cfg = {}
    if WEBAPP_SIM_CONTROLS:
        cfg["simControls"] = True
    return web.json_response(cfg, headers={"Cache-Control": "no-cache"})


async def restart_handler(request: web.Request) -> web.Response:
    """GET /restart -> kick off `innate restart` (same as the CLI) so the robot
    comes back with the latest config/settings.yaml. The restart tears down the
    tmux session this proxy runs in, so we spawn it detached with a brief delay —
    that lets this 200 flush to the browser before the proxy is killed, and the
    systemd restart job completes regardless of the client dying.

    Once detached the restart runs blind (stdout/stderr discarded, no one waits),
    so the 200 can't confirm it succeeded. The one failure we *can* catch up front
    is `innate` not being on PATH — resolve it here and 500 instead of reporting a
    false success, and spawn the absolute path so the detached `bash -c` (which
    sources no rc files) resolves it the same way we just did."""
    if request.headers.get("X-Requested-By", "") != "innate-webapp":
        raise web.HTTPForbidden(text="missing X-Requested-By header")
    innate = shutil.which("innate")
    if innate is None:
        raise web.HTTPInternalServerError(text="restart failed: `innate` not found on PATH")
    try:
        subprocess.Popen(
            ["bash", "-c", f"sleep 1; exec {innate} restart"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as err:
        raise web.HTTPInternalServerError(text=f"restart failed: {err}")
    return web.json_response({"ok": True}, headers={"Cache-Control": "no-cache"})


# The episode/run media builders do blocking disk I/O — reading multi-MB MP4s,
# h5py decodes, directory walks. Run them off the event loop so a scrubbing
# browser's back-to-back reads can't stall the rosbridge /ws relay. (episode
# itself returns a FileResponse that streams on the loop, so it needs no thread.)
def _qs(request: web.Request) -> dict:
    return parse_qs(request.query_string)


async def episode_handler(request: web.Request) -> web.StreamResponse:
    return episode_response(_qs(request))


async def joints_handler(request: web.Request) -> web.Response:
    return await asyncio.to_thread(joints_response, _qs(request))


async def profile_handler(request: web.Request) -> web.Response:
    return await asyncio.to_thread(profile_response, _qs(request))


async def thumb_handler(request: web.Request) -> web.Response:
    return await thumb_response(_qs(request))


async def map_preview_handler(request: web.Request) -> web.Response:
    return await asyncio.to_thread(map_preview_response, _qs(request))


async def run_info_handler(request: web.Request) -> web.Response:
    return await asyncio.to_thread(run_info_response, _qs(request))


async def run_log_handler(request: web.Request) -> web.Response:
    return await asyncio.to_thread(run_log_response, _qs(request))


async def settings_handler(request: web.Request) -> web.StreamResponse:
    # A WebSocket upgrade -> the write channel; a plain GET -> the override values.
    if request.headers.get("Upgrade", "").lower() == "websocket":
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await settings_ws(ws)
        return ws
    return await asyncio.to_thread(settings_get_response)


async def _pump(src: web.WebSocketResponse, dst) -> None:
    """Relay every frame from src to dst until either side closes."""
    async for msg in src:
        if msg.type == aiohttp.WSMsgType.TEXT:
            await dst.send_str(msg.data)
        elif msg.type == aiohttp.WSMsgType.BINARY:
            await dst.send_bytes(msg.data)
        else:  # CLOSE / CLOSING / ERROR — the iterator is about to stop anyway
            break


async def ws_proxy(request: web.Request) -> web.WebSocketResponse:
    """Bidirectional relay: /ws <-> rosbridge, /worldstate <-> the sim world
    server's observer stream. max_msg_size=0 lifts aiohttp's default cap for the
    large point-cloud / world-state frames."""
    ws = web.WebSocketResponse(max_msg_size=0)
    await ws.prepare(request)
    upstream_url = WORLD_STATE_URL if request.path == "/worldstate" else ROSBRIDGE_URL
    session: aiohttp.ClientSession = request.app["client"]
    try:
        async with session.ws_connect(upstream_url, max_msg_size=0) as upstream:
            tasks = [asyncio.create_task(_pump(ws, upstream)), asyncio.create_task(_pump(upstream, ws))]
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    except Exception as err:  # upstream down — close the client politely
        print(f"ws relay ended: {err}")
    finally:
        await ws.close()
    return ws


async def _on_startup(app: web.Application) -> None:
    app["client"] = aiohttp.ClientSession()


async def _on_cleanup(app: web.Application) -> None:
    await app["client"].close()


def build_app() -> web.Application:
    app = web.Application(middlewares=[compress_middleware])
    app.router.add_get("/ws", ws_proxy)
    app.router.add_get("/worldstate", ws_proxy)
    app.router.add_get("/config.json", config_handler)
    app.router.add_get("/episode", episode_handler)
    app.router.add_get("/episode/joints", joints_handler)
    app.router.add_get("/episode/profile", profile_handler)
    app.router.add_get("/episode/thumb", thumb_handler)
    app.router.add_get("/map/preview", map_preview_handler)
    app.router.add_get("/run/info", run_info_handler)
    app.router.add_get("/run/log", run_log_handler)
    app.router.add_get("/settings", settings_handler)
    app.router.add_get("/restart", restart_handler)
    # Prefix routes must precede the catch-all so /models/foo.glb doesn't fall to
    # the SPA shell — first matching resource wins in add order.
    for prefix in SIM_VIEWER_ROUTES:
        app.router.add_get(prefix + "{tail:.*}", sim_viewer_handler)
    app.router.add_get("/{tail:.*}", static_handler)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


async def main() -> None:
    _quiet_benign_disconnects()
    cert, key = ensure_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    runner = web.AppRunner(build_app(), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", HTTPS_PORT, ssl_context=ctx).start()
    print(
        f"https front door on https://0.0.0.0:{HTTPS_PORT} "
        f"(app + /ws -> {ROSBRIDGE_URL} + /worldstate -> {WORLD_STATE_URL})"
    )
    if HTTP_PORT:
        # Same app over cleartext — no auto-upgrade; the arm panel offers a manual HTTPS switch.
        await web.TCPSite(runner, "0.0.0.0", HTTP_PORT).start()
        print(f"http listener on http://0.0.0.0:{HTTP_PORT} (full app)")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
