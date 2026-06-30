#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""HTTPS front door for the Innate webapp.

Serves the static app AND proxies /ws to the local rosbridge (rws) over one
TLS port, so a single self-signed certificate acceptance gives the browser a
secure origin for everything — which is what unlocks WebSerial (leader-arm
teleop) without serving the app from the operator's laptop.

Zero dependencies beyond the `websockets` package already on the robot.
A self-signed certificate is generated on first run (10 years) under
~/.innate-webapp-tls/ via openssl.

Run:        python3 proxy/https_server.py        # https://<robot>:4443
Persist:    launched on boot in the `console-webapp` tmux window
            (innate-os/scripts/launch_ros_in_tmux.sh).
"""

import asyncio
import contextlib
import fnmatch
import json
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

# Downloaded training-run results larger than this aren't served as a single
# blob (the log viewer wants text, not gigabytes).
MAX_LOG_BYTES = 8 * 1024 * 1024

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 4443
# Plain-HTTP listener for the episode media routes only. Native clients (the
# mobile app) can't accept the self-signed TLS cert that the browser clicks
# through, so the same /episode* bytes are also served cleartext on the LAN.
# 0 disables it (env override for locked-down deployments).
HTTP_PORT = int(os.environ.get("INNATE_HTTP_PORT", "4080"))
ROOT = Path(__file__).resolve().parent.parent
CERT_DIR = Path.home() / ".innate-webapp-tls"
ROSBRIDGE_URL = "ws://127.0.0.1:9090"

# Roots the /episode* routes may serve from: workspace/custom_skills plus the
# legacy in-place locations the brain still scans ($INNATE_OS_ROOT/skills,
# ~/skills; used through 0.5.x). Deduped after resolve so INNATE_OS_ROOT=~ (which
# collapses two of them) doesn't double-check.
_INNATE_OS_ROOT = os.environ.get("INNATE_OS_ROOT", os.path.expanduser("~/innate-os"))
SKILLS_ROOTS = tuple(
    dict.fromkeys(
        p.resolve()
        for p in (
            Path(_INNATE_OS_ROOT) / "workspace" / "custom_skills",
            Path(_INNATE_OS_ROOT) / "skills",
            Path(os.path.expanduser("~")) / "skills",
        )
    )
)


def _under_skills_root(p: Path) -> bool:
    """True if p is inside an allowed skill root (path-traversal fence)."""
    return any(p.is_relative_to(root) for root in SKILLS_ROOTS)


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


def _plain(status: int, reason: str, text: str) -> Response:
    body = text.encode()
    return Response(status, reason, Headers({"Content-Type": "text/plain", "Content-Length": str(len(body))}), body)


def _resolve_under_root(rel: str):
    """Resolve a client-supplied skill directory, refusing anything that escapes
    the skills root (path-traversal guard, mirrors static_response)."""
    if not rel:
        return None
    try:
        p = Path(rel).resolve()
    except (OSError, ValueError):
        return None
    return p if _under_skills_root(p) else None


def _safe_resolve(p: Path):
    """Path.resolve() that returns None instead of raising on illegal bytes (e.g.
    a NUL byte in a query param), so malformed input becomes a 404, not a 500."""
    try:
        return p.resolve()
    except (OSError, ValueError):
        return None


def _parse_range(header, size: int):
    """Parse a single-range ``Range: bytes=start-end`` header → (start, end)
    inclusive, or None if absent/unsatisfiable (caller serves 200)."""
    if not header or not header.startswith("bytes="):
        return None
    spec = header[len("bytes=") :].split(",", 1)[0].strip()
    if "-" not in spec:
        return None
    start_s, end_s = spec.split("-", 1)
    try:
        if start_s == "":  # suffix range: last N bytes
            n = int(end_s)
            if n <= 0:
                return None
            start, end = max(0, size - n), size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
    except ValueError:
        return None
    if start > end or start >= size:
        return None
    return start, min(end, size - 1)


def _serve_file_with_range(request, path: Path, content_type: str) -> Response:
    """Serve a file honoring HTTP Range so <video> can scrub. For a range request
    we read only the requested bytes from disk (seek + read) rather than slurping
    the whole file and slicing — episode MP4s can be tens of MB and a scrubbing
    browser fires many small range reads, so reading the whole file each time is
    wasteful."""
    size = path.stat().st_size
    common = {"Content-Type": content_type, "Accept-Ranges": "bytes", "Cache-Control": "no-cache"}
    rng = _parse_range(request.headers.get("Range"), size)
    if rng is None:
        body = path.read_bytes()
        return Response(200, "OK", Headers({**common, "Content-Length": str(size)}), body)
    start, end = rng
    with open(path, "rb") as fh:
        fh.seek(start)
        chunk = fh.read(end - start + 1)
    headers = Headers({**common, "Content-Length": str(len(chunk)), "Content-Range": f"bytes {start}-{end}/{size}"})
    return Response(206, "Partial Content", headers, chunk)


def episode_response(request, qs: dict) -> Response:
    """GET /episode?dir=<skill_dir>&id=<n>&camera=<cam> → episode MP4 (Range)."""
    base = _resolve_under_root((qs.get("dir") or [""])[0])
    eid = (qs.get("id") or [""])[0]
    cam = (qs.get("camera") or [""])[0]
    if base is None or not eid or not cam:
        return _plain(404, "Not Found", "not found")
    mp4 = _safe_resolve(base / "data" / f"episode_{eid}_{cam}.mp4")
    if mp4 is None or not _under_skills_root(mp4) or mp4.suffix != ".mp4" or not mp4.is_file():
        return _plain(404, "Not Found", "no such episode video")
    return _serve_file_with_range(request, mp4, "video/mp4")


def _make_thumb(mp4_path: Path, cache_path: Path, width: int = 240) -> None:
    """Decode one representative frame from *mp4_path* and write a small JPEG to
    *cache_path* (atomically). Runs in a thread — cv2 is blocking."""
    import cv2  # available in the robot's system python (with video support)

    cap = cv2.VideoCapture(str(mp4_path))
    try:
        cap.set(cv2.CAP_PROP_POS_MSEC, 400)  # ~0.4s in for a settled frame
        ok, frame = cap.read()
        if not ok or frame is None:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = cap.read()
    finally:
        cap.release()
    if not ok or frame is None:
        raise RuntimeError("could not read a frame")

    h, w = frame.shape[:2]
    if w > width:
        frame = cv2.resize(frame, (width, max(1, round(h * width / w))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok:
        raise RuntimeError("jpeg encode failed")

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(".jpg.tmp")
    tmp.write_bytes(buf.tobytes())
    os.replace(str(tmp), str(cache_path))


# In-flight thumbnail generations, keyed by cache path, so a burst of lazy <img>
# loads on a gallery page doesn't spawn N redundant cv2 decoders for the same
# frame: the first request generates, the rest await the same lock and then read
# the now-cached file.
_thumb_locks: dict[str, asyncio.Lock] = {}


async def thumb_response(qs: dict) -> Response:
    """GET /episode/thumb?dir=<skill_dir>&id=<n>&camera=<cam> → cached JPEG of a
    frame from the episode MP4 (generated on first request, then served static)."""
    base = _resolve_under_root((qs.get("dir") or [""])[0])
    eid = (qs.get("id") or [""])[0]
    cam = (qs.get("camera") or ["camera_1"])[0]
    if base is None or not eid:
        return _plain(404, "Not Found", "not found")
    mp4 = _safe_resolve(base / "data" / f"episode_{eid}_{cam}.mp4")
    if mp4 is None or not _under_skills_root(mp4) or not mp4.is_file():
        return _plain(404, "Not Found", "no such episode video")
    # Cache beside data/ (not inside it) so thumbnails are never uploaded to the cloud.
    cache = base / "thumbs" / f"episode_{eid}_{cam}.jpg"
    try:
        if not cache.is_file():
            lock = _thumb_locks.setdefault(str(cache), asyncio.Lock())
            try:
                async with lock:
                    if not cache.is_file():  # another request may have generated it while we waited
                        await asyncio.to_thread(_make_thumb, mp4, cache)
            finally:
                # finally, so a failed _make_thumb doesn't leak the lock entry.
                _thumb_locks.pop(str(cache), None)
        data = await asyncio.to_thread(cache.read_bytes)
    except Exception as err:  # noqa: BLE001
        return _plain(500, "Internal Server Error", f"thumb failed: {err}")
    return Response(
        200,
        "OK",
        Headers({"Content-Type": "image/jpeg", "Content-Length": str(len(data)), "Cache-Control": "max-age=86400"}),
        data,
    )


def joints_response(qs: dict) -> Response:
    """GET /episode/joints?dir=<skill_dir>&id=<n> → qpos/qvel/timestamps JSON,
    read straight from the (possibly image-stripped) HDF5 — joints are kept."""
    base = _resolve_under_root((qs.get("dir") or [""])[0])
    eid = (qs.get("id") or [""])[0]
    if base is None or not eid:
        return _plain(404, "Not Found", "not found")
    h5 = _safe_resolve(base / "data" / f"episode_{eid}.h5")
    if h5 is None or not _under_skills_root(h5) or h5.suffix != ".h5" or not h5.is_file():
        return _plain(404, "Not Found", "no such episode")
    try:
        import h5py  # available in the robot's system python

        with h5py.File(str(h5), "r") as f:
            obs = f["observations"]
            qpos = obs["qpos"][:].tolist() if "qpos" in obs else []
            qvel = obs["qvel"][:].tolist() if "qvel" in obs else []
            ts = []
            if "timestamps" in f and "arm" in f["timestamps"]:
                ts = f["timestamps"]["arm"][:].tolist()
        freq = 0
        meta = base / "data" / "dataset_metadata.json"
        if meta.is_file():
            freq = json.loads(meta.read_text()).get("data_frequency", 0)
        payload = json.dumps({"qpos": qpos, "qvel": qvel, "timestamps": ts, "data_frequency": freq}).encode()
    except Exception as err:  # noqa: BLE001 — surface a clean 500 to the client
        return _plain(500, "Internal Server Error", f"failed to read joints: {err}")
    return Response(
        200,
        "OK",
        Headers({"Content-Type": "application/json", "Content-Length": str(len(payload)), "Cache-Control": "no-cache"}),
        payload,
    )


def run_info_response(qs: dict) -> Response:
    """GET /run/info?dir=<skill_dir>&id=<run_id> → downloaded?/has_checkpoint?/files.
    A run is 'successful' if its downloaded results contain a *_step_*.pth — the
    same check the training node uses to activate a checkpoint."""
    base = _resolve_under_root((qs.get("dir") or [""])[0])
    rid = (qs.get("id") or [""])[0]
    if base is None or not rid:
        return _plain(404, "Not Found", "not found")
    run_dir = _safe_resolve(base / rid)
    if run_dir is None or not _under_skills_root(run_dir) or not run_dir.is_dir():
        # Not downloaded yet (or never will be).
        body = json.dumps({"downloaded": False, "has_checkpoint": False, "files": []}).encode()
        return Response(
            200, "OK", Headers({"Content-Type": "application/json", "Content-Length": str(len(body))}), body
        )
    files = []
    has_ckpt = False
    truncated = False
    max_files = 2000  # bound the response — run dirs can hold many checkpoint shards
    try:
        for p in sorted(run_dir.rglob("*")):
            if not p.is_file():
                continue
            if fnmatch.fnmatch(p.name, "*_step_*.pth"):
                has_ckpt = True
            if len(files) < max_files:
                files.append(p.relative_to(run_dir).as_posix())
            else:
                truncated = True
    except OSError as err:
        return _plain(500, "Internal Server Error", f"failed to read run dir: {err}")
    body = json.dumps({"downloaded": True, "has_checkpoint": has_ckpt, "files": files, "truncated": truncated}).encode()
    return Response(
        200,
        "OK",
        Headers({"Content-Type": "application/json", "Content-Length": str(len(body)), "Cache-Control": "no-cache"}),
        body,
    )


def run_log_response(qs: dict) -> Response:
    """GET /run/log?dir=<skill_dir>&id=<run_id>&file=<relpath> → a run log file
    as text/plain. Sandboxed to the run directory."""
    base = _resolve_under_root((qs.get("dir") or [""])[0])
    rid = (qs.get("id") or [""])[0]
    rel = (qs.get("file") or [""])[0]
    if base is None or not rid or not rel:
        return _plain(404, "Not Found", "not found")
    run_dir = _safe_resolve(base / rid)
    target = _safe_resolve(run_dir / rel) if run_dir else None
    if (
        run_dir is None
        or target is None
        or not _under_skills_root(run_dir)
        or not target.is_relative_to(run_dir)
        or not target.is_file()
    ):
        return _plain(404, "Not Found", "no such log file")
    try:
        # Bound the read: this route serves *any* file under the run dir (the
        # guard only checks containment + is_file()), including multi-GB .pth
        # checkpoints — never pull more than the cap into RAM. read() of a
        # too-large file returns the cap+1 so the truncation check below still
        # fires, but peak memory is bounded regardless of file size.
        with open(target, "rb") as fh:
            data = fh.read(MAX_LOG_BYTES + 1)
    except OSError as err:
        return _plain(500, "Internal Server Error", f"read failed: {err}")
    truncated = b""
    if len(data) > MAX_LOG_BYTES:
        data = data[:MAX_LOG_BYTES]
        truncated = b"\n\n[truncated]\n"
    body = data + truncated
    return Response(
        200,
        "OK",
        Headers(
            {"Content-Type": "text/plain; charset=utf-8", "Content-Length": str(len(body)), "Cache-Control": "no-cache"}
        ),
        body,
    )


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


async def process_request(connection, request):
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


async def process_media_request(connection, request):
    """Plain-HTTP listener: ONLY the read-only episode media routes, for native
    clients that won't accept the self-signed TLS cert. No /ws, no app shell, no
    run logs — those stay TLS-only on the front door."""
    split = urlsplit(request.path)
    qs = parse_qs(split.query)
    if split.path == "/episode":
        return await asyncio.to_thread(episode_response, request, qs)
    if split.path == "/episode/joints":
        return await asyncio.to_thread(joints_response, qs)
    if split.path == "/episode/thumb":
        return await thumb_response(qs)
    return _plain(404, "Not Found", "not found")


async def _reject_ws(connection):
    """The media listener never upgrades to WebSocket (process_media_request
    always returns a Response), but serve() needs a handler — close politely."""
    await connection.close()


async def relay(source, sink):
    try:
        async for message in source:
            await sink.send(message)
    except ConnectionClosed:
        pass  # either side going away ends the relay — a normal close, not an error


async def _settings_ws(connection):
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


async def ws_handler(connection):
    """Bidirectional /ws <-> rosbridge relay (or the /settings write channel)."""
    if connection.request.path == "/settings":
        await _settings_ws(connection)
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
            serve(
                ws_handler,
                "0.0.0.0",
                PORT,
                ssl=ctx,
                process_request=process_request,
                max_size=None,
            )
        )
        print(f"https front door on https://0.0.0.0:{PORT} (app + /ws -> {ROSBRIDGE_URL})")
        if HTTP_PORT:
            await stack.enter_async_context(
                serve(
                    _reject_ws,
                    "0.0.0.0",
                    HTTP_PORT,
                    process_request=process_media_request,
                    max_size=None,
                )
            )
            print(f"http media listener on http://0.0.0.0:{HTTP_PORT} (/episode* only)")
        await asyncio.get_running_loop().create_future()


if __name__ == "__main__":
    asyncio.run(main())
