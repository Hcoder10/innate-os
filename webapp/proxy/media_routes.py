"""Episode + training-run read endpoints for the webapp front door.

Pure, read-only HTTP handlers served by https_server.py (over TLS) and by the
plain-HTTP media listener. Every file access is fenced to the skill roots below
(path-traversal guards). Split out of https_server.py.
"""

import asyncio
import fnmatch
import json
import os
import re
from pathlib import Path

from websockets.datastructures import Headers
from websockets.http11 import Response

# Downloaded training-run results larger than this aren't served as a single
# blob (the log viewer wants text, not gigabytes).
MAX_LOG_BYTES = 8 * 1024 * 1024

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


# A run's own exception line, e.g. "RuntimeError: stack expects each tensor to
# be equal size...". Anchored to the exception-name shape rather than a bare
# "error" substring so progress lines mentioning errors don't match.
_ERROR_LINE_RE = re.compile(r"^\s*(?:[\w.]+\.)?[A-Z]\w*(?:Error|Exception|Interrupt)\b.*|^\s*(?:FATAL|fatal error)\b.*")
_TAIL_BYTES = 128 * 1024  # errors live at the end; don't read multi-MB logs whole


def _failure_excerpt(run_dir) -> str:
    """Last exception-looking line from the run's logs, or "".

    Scans the tail of the same files the Logs modal prefers. jsonl lines are
    {"line": ..., "stream": ...}; plain logs are read as-is. The *last* match
    wins — a traceback's final line names the actual exception.
    """
    excerpt = ""
    for name in ("process_output.jsonl", "daemon.log", "output.log"):
        path = run_dir / name
        if not path.is_file():
            continue
        try:
            with open(path, "rb") as fh:
                fh.seek(max(0, path.stat().st_size - _TAIL_BYTES))
                tail = fh.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        for raw in tail.splitlines():
            line = raw
            if name.endswith(".jsonl"):
                try:
                    line = str(json.loads(raw).get("line", ""))
                except (json.JSONDecodeError, AttributeError):
                    continue
            if _ERROR_LINE_RE.match(line):
                excerpt = line.strip()
        if excerpt:
            return excerpt[:400]
    return ""


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
    # Failed run (no checkpoint): pull the actual exception line out of the
    # downloaded logs so the Training page can say WHY, not just "no checkpoint".
    error_excerpt = "" if has_ckpt else _failure_excerpt(run_dir)
    body = json.dumps(
        {
            "downloaded": True,
            "has_checkpoint": has_ckpt,
            "files": files,
            "truncated": truncated,
            "error_excerpt": error_excerpt,
        }
    ).encode()
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
