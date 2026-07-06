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
itself (TLS, static, dispatch, the /ws relay). It serves the same app on both a
TLS port (HTTPS) and a cleartext port (HTTP). The secure-origin features
(WebSerial leader-arm) need HTTPS; the arm panel offers a one-click switch
rather than an automatic bounce. A self-signed certificate is generated on first
run (10 years) under ~/.innate-webapp-tls/ via openssl.

Run:        python3 proxy/https_server.py        # https://<robot>:443 + http://<robot>:80
Persist:    launched on boot in the `console-webapp` tmux window
            (innate-os/scripts/launch_ros_in_tmux.sh).
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import mimetypes
import os
import shutil
import ssl
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from media_routes import (
    _plain,
    episode_response,
    joints_response,
    profile_response,
    run_info_response,
    run_log_response,
    thumb_response,
)
from settings_routes import settings_get_response, settings_ws
from websockets.asyncio.client import connect as ws_connect
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response

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


def _quiet_benign_disconnects() -> None:
    """Stop the websockets library from logging a full traceback every time a
    client drops the TCP connection during the opening handshake (browser
    reloads, cert-warning preconnects, port scanners). Those raise
    ConnectionClosed *before* ws_handler runs, so we can't catch them there;
    instead filter just that exception type out of the library's logs while
    keeping every other error. Attached to a handler so it also applies to
    records propagated from per-connection child loggers.
    """

    class _DropClosed(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            exc = record.exc_info[1] if record.exc_info else None
            return not isinstance(exc, ConnectionClosed)

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    handler.addFilter(_DropClosed())
    log = logging.getLogger("websockets")
    log.handlers.clear()
    log.addHandler(handler)
    log.setLevel(logging.WARNING)
    log.propagate = False


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


def sim_viewer_response(path: str) -> "Response | None":
    clean = path.split("?", 1)[0].split("#", 1)[0]
    for prefix, base in SIM_VIEWER_ROUTES.items():
        if not clean.startswith(prefix):
            continue
        target = (base / clean[len(prefix) :]).resolve()
        if not target.is_file() or not target.is_relative_to(base.resolve()):
            return Response(404, "Not Found", Headers({"Content-Type": "text/plain"}), b"not found")
        body = target.read_bytes()
        ctype = CONTENT_TYPES.get(target.suffix) or mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        headers = Headers({"Content-Type": ctype, "Content-Length": str(len(body)), "Cache-Control": "no-cache"})
        return Response(200, "OK", headers, body)
    return None


def static_response(path: str, if_none_match: str = "") -> Response:
    clean = path.split("?", 1)[0].split("#", 1)[0]
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
        return Response(404, "Not Found", Headers({"Content-Type": "text/plain"}), b"not found")
    body = body_path.read_bytes()
    # Content-hash ETag: the first load (and any hard refresh) requests all ~27
    # modules, but unchanged ones come back as a bodyless 304 instead of a full
    # re-download. Cache-Control stays no-cache so a redeploy is always picked up
    # — the browser still revalidates every load, but revalidation is now a tiny
    # conditional request, not a transfer of the whole file.
    etag = f'"{hashlib.sha1(body).hexdigest()}"'
    if if_none_match == etag:
        return Response(304, "Not Modified", Headers({"ETag": etag, "Cache-Control": "no-cache"}), b"")
    ctype = CONTENT_TYPES.get(body_path.suffix) or mimetypes.guess_type(str(body_path))[0] or "application/octet-stream"
    headers = Headers(
        {
            "Content-Type": ctype,
            "Content-Length": str(len(body)),
            "Cache-Control": "no-cache",
            "ETag": etag,
        }
    )
    return Response(200, "OK", headers, body)


def config_response() -> Response:
    """Serve config.json with env-driven feature flags overlaid, so a deployment
    can flip flags without editing the committed file (the sim sets
    WEBAPP_SIM_CONTROLS=1)."""
    try:
        cfg = json.loads((ROOT / "config.json").read_text())
    except Exception:
        cfg = {}
    if WEBAPP_SIM_CONTROLS:
        cfg["simControls"] = True
    body = json.dumps(cfg).encode()
    return Response(
        200,
        "OK",
        Headers({"Content-Type": "application/json", "Content-Length": str(len(body)), "Cache-Control": "no-cache"}),
        body,
    )


def restart_response() -> Response:
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
    innate = shutil.which("innate")
    if innate is None:
        return _plain(500, "Internal Server Error", "restart failed: `innate` not found on PATH")
    try:
        subprocess.Popen(
            ["bash", "-c", f"sleep 1; exec {innate} restart"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as err:
        return _plain(500, "Internal Server Error", f"restart failed: {err}")
    body = json.dumps({"ok": True}).encode()
    return Response(
        200,
        "OK",
        Headers({"Content-Type": "application/json", "Content-Length": str(len(body)), "Cache-Control": "no-cache"}),
        body,
    )


async def _dispatch_request(connection, request):
    if request.path == "/ws":
        return None  # proceed with the WebSocket handshake
    if request.path.split("?", 1)[0] == "/config.json":
        return await asyncio.to_thread(config_response)
    sim_viewer = await asyncio.to_thread(sim_viewer_response, request.path)
    if sim_viewer is not None:
        return sim_viewer
    split = urlsplit(request.path)
    qs = parse_qs(split.query)
    # These builders do blocking disk I/O — reading multi-MB MP4s, h5py decodes,
    # directory walks. Run them off the event loop (to_thread) so a scrubbing
    # browser's back-to-back range reads can't stall the rosbridge /ws relay.
    if split.path == "/episode":
        return await asyncio.to_thread(episode_response, request, qs)
    if split.path == "/episode/joints":
        return await asyncio.to_thread(joints_response, qs)
    if split.path == "/episode/profile":
        return await asyncio.to_thread(profile_response, qs)
    if split.path == "/episode/thumb":
        return await thumb_response(qs)
    if split.path == "/run/info":
        return await asyncio.to_thread(run_info_response, qs)
    if split.path == "/run/log":
        return await asyncio.to_thread(run_log_response, qs)
    if split.path == "/settings":
        # A WebSocket upgrade -> the write channel handled in ws_handler; a plain
        # GET -> the current override values.
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return None
        return await asyncio.to_thread(settings_get_response)
    if split.path == "/restart":
        if request.headers.get("X-Requested-By", "") != "innate-webapp":
            return _plain(403, "Forbidden", "missing X-Requested-By header")
        return await asyncio.to_thread(restart_response)
    return await asyncio.to_thread(static_response, request.path, request.headers.get("If-None-Match", ""))


async def relay(source, sink):
    try:
        async for message in source:
            await sink.send(message)
    except ConnectionClosed:
        pass  # either side going away ends the relay — a normal close, not an error


async def ws_handler(connection):
    """Bidirectional /ws <-> rosbridge relay (or the /settings write channel)."""
    if connection.request.path == "/settings":
        await settings_ws(connection)
        return
    try:
        async with ws_connect(ROSBRIDGE_URL, max_size=None) as upstream:
            done, pending = await asyncio.wait(
                [
                    asyncio.ensure_future(relay(connection, upstream)),
                    asyncio.ensure_future(relay(upstream, connection)),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            # Consume every task's result so a relay that ended on a benign client
            # disconnect (1001 going away) doesn't surface as an asyncio
            # "Task exception was never retrieved" warning.
            await asyncio.gather(*done, *pending, return_exceptions=True)
    except Exception as err:  # upstream down — close the client politely
        print(f"ws relay ended: {err}")
    finally:
        await connection.close()


async def main():
    _quiet_benign_disconnects()
    cert, key = ensure_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    async with contextlib.AsyncExitStack() as stack:
        await stack.enter_async_context(
            serve(ws_handler, "0.0.0.0", HTTPS_PORT, ssl=ctx, process_request=_dispatch_request, max_size=None)
        )
        print(f"https front door on https://0.0.0.0:{HTTPS_PORT} (app + /ws -> {ROSBRIDGE_URL})")
        if HTTP_PORT:
            # Same app over cleartext — no auto-upgrade; the arm panel offers a manual HTTPS switch.
            await stack.enter_async_context(
                serve(ws_handler, "0.0.0.0", HTTP_PORT, process_request=_dispatch_request, max_size=None)
            )
            print(f"http listener on http://0.0.0.0:{HTTP_PORT} (full app)")
        await asyncio.get_running_loop().create_future()


if __name__ == "__main__":
    asyncio.run(main())
