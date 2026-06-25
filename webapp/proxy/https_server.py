#!/usr/bin/env python3
"""HTTPS front door for the Innate webapp.

Serves the static app AND proxies /ws to the local rosbridge (rws) over one
TLS port, so a single self-signed certificate acceptance gives the browser a
secure origin for everything — which is what unlocks WebSerial (leader-arm
teleop) without serving the app from the operator's laptop.

The read-only episode/run media endpoints live in media_routes.py and the
settings read/write endpoints in settings_routes.py; this module is the server
itself (TLS, static, dispatch, the /ws relay). It serves the same app on both a
TLS port (HTTPS) and a cleartext port (HTTP) — the frontend redirects http ->
https for the secure-origin features (WebSerial). A self-signed certificate is
generated on first run (10 years) under ~/.innate-webapp-tls/ via openssl.

Run:        python3 proxy/https_server.py        # https://<robot>:443 + http://<robot>:80
Persist:    launched on boot in the `console-webapp` tmux window
            (innate-os/scripts/launch_ros_in_tmux.sh).
"""

import asyncio
import contextlib
import logging
import mimetypes
import os
import ssl
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.client import connect as ws_connect
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response

from media_routes import (
    _plain,
    episode_response,
    joints_response,
    run_info_response,
    run_log_response,
    thumb_response,
)
from settings_routes import settings_get_response, settings_ws

HTTPS_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 443
# Cleartext HTTP listener serving the SAME app as the TLS front door, so the site
# is reachable over http://<robot> too; the frontend redirects http -> https for
# the secure-origin features (WebSerial). Native clients (the mobile app) that
# can't accept the self-signed cert also reach /episode* here. 0 disables it.
# NOTE: 80/443 are privileged — bind needs root or cap_net_bind_service.
HTTP_PORT = int(os.environ.get("INNATE_HTTP_PORT", "80"))
ROOT = Path(__file__).resolve().parent.parent
CERT_DIR = Path.home() / ".innate-webapp-tls"
ROSBRIDGE_URL = "ws://127.0.0.1:9090"

# Sticky https upgrade (trust-on-first-use). We can't hard-redirect every http
# hit to https: the cert is self-signed, so a first-visit redirect drops the user
# on a bare cert warning. Instead, once a browser has loaded over https (and thus
# accepted the cert) we set this cookie there, and from then on bounce its
# cleartext hits to https. Not Secure (must be sent over http to be seen there);
# HttpOnly + Lax since only the server reads it.
HTTPS_COOKIE = "https_seen"
_COOKIE_SET = f"{HTTPS_COOKIE}=1; Path=/; Max-Age=31536000; SameSite=Lax; HttpOnly"


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
}


def static_response(path: str) -> Response:
    clean = path.split("?", 1)[0].split("#", 1)[0]
    target = (ROOT / clean.lstrip("/")).resolve()
    if target.is_dir():
        target = target / "index.html"
    body_path = target if target.is_file() else None
    # Refuse anything that escapes the app root (or the TLS keys, defensively).
    if body_path is None or not body_path.is_relative_to(ROOT) or body_path.suffix == ".pem":
        return Response(404, "Not Found", Headers({"Content-Type": "text/plain"}), b"not found")
    body = body_path.read_bytes()
    ctype = CONTENT_TYPES.get(body_path.suffix) or mimetypes.guess_type(str(body_path))[0] or "application/octet-stream"
    headers = Headers(
        {
            "Content-Type": ctype,
            "Content-Length": str(len(body)),
            "Cache-Control": "no-cache",
        }
    )
    return Response(200, "OK", headers, body)


async def _dispatch_request(connection, request):
    if request.path == "/ws":
        return None  # proceed with the WebSocket handshake
    split = urlsplit(request.path)
    qs = parse_qs(split.query)
    # These builders do blocking disk I/O — reading multi-MB MP4s, h5py decodes,
    # directory walks. Run them off the event loop (to_thread) so a scrubbing
    # browser's back-to-back range reads can't stall the rosbridge /ws relay.
    if split.path == "/episode":
        return await asyncio.to_thread(episode_response, request, qs)
    if split.path == "/episode/joints":
        return await asyncio.to_thread(joints_response, qs)
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
    return await asyncio.to_thread(static_response, request.path)


def _is_secure(connection) -> bool:
    """True if this connection arrived over TLS (the https listener)."""
    transport = getattr(connection, "transport", None)
    return bool(transport and transport.get_extra_info("ssl_object"))


def _has_https_cookie(request) -> bool:
    """The browser has previously loaded over https (cookie set there), so it has
    accepted the self-signed cert and we can safely send it back to https."""
    return any(part.strip() == f"{HTTPS_COOKIE}=1" for part in request.headers.get("Cookie", "").split(";"))


def _redirect_to_https(request):
    host = request.headers.get("Host", "").rsplit(":", 1)[0] or "0.0.0.0"
    netloc = host if HTTPS_PORT == 443 else f"{host}:{HTTPS_PORT}"
    location = f"https://{netloc}{request.path}"
    return Response(307, "Temporary Redirect", Headers({"Location": location, "Content-Length": "0"}), b"")


async def process_request(connection, request):
    secure = _is_secure(connection)
    is_ws = request.headers.get("Upgrade", "").lower() == "websocket"
    # Sticky upgrade: bounce a cleartext hit to https once the browser is known to
    # have accepted the cert. Never redirect a WebSocket upgrade (clients don't
    # follow 3xx) — but if the cookie is set the page is already https anyway.
    if not secure and not is_ws and _has_https_cookie(request):
        return _redirect_to_https(request)
    response = await _dispatch_request(connection, request)
    # Remember https-capable browsers so the redirect above can fire next time.
    if secure and response is not None:
        response.headers["Set-Cookie"] = _COOKIE_SET
    return response


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
            serve(ws_handler, "0.0.0.0", HTTPS_PORT, ssl=ctx, process_request=process_request, max_size=None)
        )
        print(f"https front door on https://0.0.0.0:{HTTPS_PORT} (app + /ws -> {ROSBRIDGE_URL})")
        if HTTP_PORT:
            # Same app over cleartext; the frontend redirects browsers to https.
            await stack.enter_async_context(
                serve(ws_handler, "0.0.0.0", HTTP_PORT, process_request=process_request, max_size=None)
            )
            print(f"http listener on http://0.0.0.0:{HTTP_PORT} (full app; frontend redirects to https)")
        await asyncio.get_running_loop().create_future()


if __name__ == "__main__":
    asyncio.run(main())
