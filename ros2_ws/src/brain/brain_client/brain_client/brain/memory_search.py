# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Recall over the spatial memory: which remembered view answers a request.

Mobility-VLA-style retrieval: every remembered frame goes to Gemini, labeled
with its number, capture time, and map pose, and the model picks the one that
best serves the query — directly ("the kitchen") or by reasoning ("I am
hungry"). The frames ride in an explicit Gemini context cache, so a search
uploads only the question; the cache is rebuilt lazily whenever the memory
changes, expires, or the process restarts, and :meth:`warm` refreshes it in
the background so the first search stays fast. A backend without the
cachedContents endpoint falls back to inline frames — slower, same answers.

Every search concludes in a :class:`SearchVerdict` — one structured outcome
for every consumer: the agent's fire-and-forget event path (the loop must
never block on the network), the ``/brain/search_memory`` action server that
skills call, and the webapp mirror. :func:`verdict_text` renders the one
model-facing sentence for all of them.
"""

from __future__ import annotations

import base64
import contextlib
import json
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from brain_client.brain.transport import CACHED_CONTENTS_PATH, GENERATE_PATH, GeminiHttpError

if TYPE_CHECKING:
    from collections.abc import Callable

    from brain_client.brain.transport import GeminiRest
    from brain_client.memory.store import Memory, MemorySnapshot, MemoryStore

_TTL_SEC = 12 * 3600  # ~20k-token cache: 12h of storage costs ~$0.24, cheap insurance against cold starts
_TTL_SAFETY_SEC = 120  # treat the cache as gone this long before Gemini actually expires it
_MIN_FRAMES_TO_CACHE = 6  # fewer frames sit under the API's minimum cache size (and upload fast anyway)
_WARM_AFTER_QUIET_SEC = 30.0  # let a recording burst settle before re-uploading every frame
_UNSUPPORTED_STATUSES = (404, 405, 501)  # the backend has no cachedContents passthrough

_SYSTEM = (
    "You are the spatial memory of a small home robot. You hold snapshots the robot remembered "
    "while driving around its current map; each frame is labeled with its number, when it was "
    "recorded, and the map pose it was taken from. Given what the robot needs, pick the single "
    "frame whose view best serves it — the place itself, or where the needed thing was last "
    'seen. Needs may be indirect: "I am hungry" points at food or the kitchen, "exit the '
    'room" at a doorway. Prefer the most recent frame among equals. If no remembered view '
    "plausibly helps, be honest and report no match."
)

_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "found": {"type": "BOOLEAN"},
        "frame": {"type": "INTEGER", "description": "number of the best frame, 0 when found is false"},
        "explanation": {
            "type": "STRING",
            "description": "one sentence: what the frame shows and why it serves the need",
        },
    },
    "required": ["found", "frame", "explanation"],
}

_GENERATION_CONFIG = {
    "responseMimeType": "application/json",
    "responseSchema": _RESPONSE_SCHEMA,
    "thinkingConfig": {"thinkingLevel": "low"},
}


@dataclass(frozen=True)
class _CacheHandle:
    name: str  # "cachedContents/…"
    revision: int
    map_name: str | None
    memories: tuple[Memory, ...]  # frame numbers resolve against what was cached, not the live store
    expires_monotonic: float


@dataclass(frozen=True)
class SearchVerdict:
    """One search's structured outcome. ``error`` non-empty means the search
    itself failed (transport, unreadable answer) — distinct from a clean
    no-match, which is ``found=False`` with an explanation."""

    query: str
    found: bool
    explanation: str = ""
    error: str = ""
    memory: Memory | None = None
    image: bytes | None = None
    latency_sec: float = 0.0
    cached: bool = False


class MemorySearch:
    def __init__(self, store: MemoryStore, rest: GeminiRest, *, model: str, logger):
        self._store = store
        self._rest = rest
        self._model = model
        self._logger = logger
        self._cache: _CacheHandle | None = None
        self._cache_unsupported = False
        self._failed_revision: int | None = None  # cache creation failed for exactly this content; don't hammer
        self._flight = threading.Lock()  # one network operation at a time (a search or a warm)
        # UI mirror, set by the composition root: every finished search's verdict
        # as a JSON-able dict (query, found, pose, explanation, latency, cached).
        self.on_result: Callable[[dict], None] | None = None

    @property
    def frame_count(self) -> int:
        return len(self._store.snapshot().memories)

    def cache_state(self) -> str:
        """How the next search will run: warm | cold | inline | unsupported."""
        if self._cache_unsupported:
            return "unsupported"
        snapshot = self._store.snapshot()
        if len(snapshot.memories) < _MIN_FRAMES_TO_CACHE:
            return "inline"  # under the cache floor — always answered inline, still quick
        return "warm" if self._cache_matches(snapshot) else "cold"

    def begin_search(self, query: str, on_done: Callable[[SearchVerdict], None]) -> None:
        """Run a search on a daemon thread; the verdict hits ``on_done`` when it lands."""

        def run() -> None:
            try:
                on_done(self.search(query))
            except Exception as error:  # noqa: BLE001 — a broken consumer must not kill the thread silently
                self._logger.error(f"[Memory] search callback failed: {error!r}")

        threading.Thread(target=run, name="memory-search", daemon=True).start()

    def search(self, query: str) -> SearchVerdict:
        """Blocking: ask Gemini which remembered frame serves the query.

        Never raises — failures come back as an ``error`` verdict every
        consumer (agent event, action result, webapp card) can render.
        """
        with self._flight:
            started = time.monotonic()
            try:
                verdict = self._search_locked(query, started)
            except Exception as error:  # noqa: BLE001 — transport failures become a typed error verdict
                self._logger.error(f"[Memory] search failed: {error!r}")
                verdict = SearchVerdict(query=query, found=False, error=str(error))
        self._report(verdict)
        return verdict

    def _search_locked(self, query: str, started: float) -> SearchVerdict:
        snapshot = self._store.snapshot()
        if not snapshot.memories:
            return SearchVerdict(
                query=query,
                found=False,
                explanation="The robot has no memories of this map yet — drive around in navigation mode to build them.",
            )
        cache = self._ensure_cache(snapshot)
        if cache is not None:
            body = {
                "cachedContent": cache.name,
                "contents": [_user([{"text": _question(query)}])],
                "generationConfig": _GENERATION_CONFIG,
            }
            try:
                response = self._rest.post(GENERATE_PATH.format(model=self._model), body)
                return self._conclude(query, response, cache.memories, started, cached=True)
            except GeminiHttpError as error:
                if error.status >= 500:
                    raise
                # The cache died server-side (expired early, deleted, rejected):
                # forget it and answer inline; the next warm rebuilds it.
                self._cache = None
                self._logger.warn(f"[Memory] cached search failed ({error}); retrying inline")
        frames = self._load_frames(snapshot.memories)
        body = {
            "systemInstruction": {"parts": [{"text": _SYSTEM}]},
            "contents": [_user([*_frame_parts(frames), {"text": _question(query)}])],
            "generationConfig": _GENERATION_CONFIG,
        }
        response = self._rest.post(GENERATE_PATH.format(model=self._model), body)
        return self._conclude(query, response, tuple(memory for memory, _ in frames), started, cached=False)

    def warm(self) -> None:
        """Refresh the context cache in the background once the memory has settled.

        Called from the recorder's tick; returns immediately. Keeps the first
        search after a boot or a recording burst from paying the frame upload.
        """
        snapshot = self._store.snapshot()
        if not self._should_warm(snapshot):
            return
        if time.monotonic() - self._store.last_change_monotonic < _WARM_AFTER_QUIET_SEC:
            return
        if not self._flight.acquire(blocking=False):
            return  # a search or another warm is already on the wire

        def run() -> None:
            try:
                self._ensure_cache(self._store.snapshot())
            except Exception as error:  # noqa: BLE001 — warming is opportunistic; the search path retries
                self._logger.warn(f"[Memory] cache warm failed: {error!r}")
            finally:
                self._flight.release()

        threading.Thread(target=run, name="memory-warm", daemon=True).start()

    # --- cache management (under _flight, except the read-only probes) ---
    def _cache_matches(self, snapshot: MemorySnapshot) -> bool:
        cache = self._cache
        return (
            cache is not None
            and cache.revision == snapshot.revision
            and cache.map_name == snapshot.map_name
            and time.monotonic() < cache.expires_monotonic
        )

    def _should_warm(self, snapshot: MemorySnapshot) -> bool:
        if self._cache_unsupported or len(snapshot.memories) < _MIN_FRAMES_TO_CACHE:
            return False
        if self._failed_revision == snapshot.revision:
            return False
        return not self._cache_matches(snapshot)

    def _ensure_cache(self, snapshot: MemorySnapshot) -> _CacheHandle | None:
        if self._cache_unsupported or len(snapshot.memories) < _MIN_FRAMES_TO_CACHE:
            return None
        if self._cache_matches(snapshot):
            return self._cache
        if self._failed_revision == snapshot.revision:
            return None
        return self._create_cache(snapshot)

    def _create_cache(self, snapshot: MemorySnapshot) -> _CacheHandle | None:
        frames = self._load_frames(snapshot.memories)
        if len(frames) < _MIN_FRAMES_TO_CACHE:
            return None
        body = {
            "model": f"models/{self._model}",
            "systemInstruction": {"parts": [{"text": _SYSTEM}]},
            "contents": [_user(_frame_parts(frames))],
            "ttl": f"{_TTL_SEC}s",
            "displayName": f"mars-spatial-memory-{snapshot.map_name or 'unknown'}",
        }
        started = time.monotonic()
        try:
            name = str(self._rest.post(CACHED_CONTENTS_PATH, body).get("name") or "")
        except GeminiHttpError as error:
            if error.status in _UNSUPPORTED_STATUSES:
                self._cache_unsupported = True
                self._logger.warn(
                    f"[Memory] backend has no context-cache endpoint (HTTP {error.status}); searches run uncached"
                )
            else:
                self._failed_revision = snapshot.revision
                self._logger.warn(f"[Memory] context cache creation failed: {error}")
            return None
        if not name:
            self._failed_revision = snapshot.revision
            return None
        old, self._cache = (
            self._cache,
            _CacheHandle(
                name=name,
                revision=snapshot.revision,
                map_name=snapshot.map_name,
                memories=tuple(memory for memory, _ in frames),
                expires_monotonic=started + _TTL_SEC - _TTL_SAFETY_SEC,
            ),
        )
        if old is not None:
            with contextlib.suppress(Exception):
                self._rest.delete(f"/v1beta/{old.name}")
        self._logger.info(f"[Memory] cached {len(frames)} frames for search in {time.monotonic() - started:.1f}s")
        return self._cache

    # --- request assembly / response handling ---
    def _conclude(
        self, query: str, response: dict, memories: tuple[Memory, ...], started: float, *, cached: bool
    ) -> SearchVerdict:
        latency = round(time.monotonic() - started, 2)
        parsed = _parse_verdict(response)
        if parsed is None:
            return SearchVerdict(
                query=query, found=False, error="unreadable answer", latency_sec=latency, cached=cached
            )
        found, frame, explanation = parsed
        if not found or not 1 <= frame <= len(memories):
            return SearchVerdict(query=query, found=False, explanation=explanation, latency_sec=latency, cached=cached)
        memory = memories[frame - 1]
        return SearchVerdict(
            query=query,
            found=True,
            explanation=explanation,
            memory=memory,
            image=self._read_image(memory),
            latency_sec=latency,
            cached=cached,
        )

    def _report(self, verdict: SearchVerdict) -> None:
        """Mirror a verdict to the UI topic; best-effort — it must never break a search."""
        if self.on_result is None:
            return
        payload: dict = {"query": verdict.query, "found": verdict.found, "stamp": time.time()}
        if verdict.error:
            payload["error"] = verdict.error
        else:
            payload |= {
                "explanation": verdict.explanation,
                "latency_sec": verdict.latency_sec,
                "cached": verdict.cached,
            }
        if verdict.memory is not None:
            memory = verdict.memory
            payload |= {
                "id": memory.id,
                "x": round(memory.x, 3),
                "y": round(memory.y, 3),
                "theta": round(memory.theta, 4),
                "seen_stamp": memory.stamp,
            }
        try:
            self.on_result(payload)
        except Exception as error:  # noqa: BLE001 — the UI mirror must not break the search
            self._logger.warn(f"[Memory] search result mirror failed: {error!r}")

    def _load_frames(self, memories: tuple[Memory, ...]) -> list[tuple[Memory, bytes]]:
        frames = []
        for memory in memories:
            jpeg = self._read_image(memory)
            if jpeg:  # evicted between snapshot and read: the frame just drops out
                frames.append((memory, jpeg))
        return frames

    def _read_image(self, memory: Memory) -> bytes | None:
        path = self._store.image_path(memory.id)
        try:
            return path.read_bytes() if path is not None else None
        except OSError:
            return None


def verdict_text(verdict: SearchVerdict) -> str:
    """The one model-facing sentence for a verdict — shared by the agent's
    event path and the search skill, so the model reads the same thing
    whichever door the search came through."""
    if verdict.error:
        return f'Memory search for "{verdict.query}" failed: {verdict.error}'
    if verdict.memory is None:
        return f'Memory search for "{verdict.query}": nothing in the remembered views matches. {verdict.explanation}'
    memory = verdict.memory
    return (
        f'Memory search for "{verdict.query}": found a match, recorded {_age_text(time.time() - memory.stamp)} ago '
        f"at map position x={memory.x:.2f}m y={memory.y:.2f}m heading={math.degrees(memory.theta):.0f}°. "
        f"{verdict.explanation} "
        + ("The attached image is that memory. " if verdict.image else "")
        + f"To go there: navigate_to_position(x={memory.x:.2f}, y={memory.y:.2f}, "
        f"theta_degrees={math.degrees(memory.theta):.0f}, local_frame=false)."
    )


def _user(parts: list[dict]) -> dict:
    return {"role": "user", "parts": parts}


def _frame_parts(frames: list[tuple[Memory, bytes]]) -> list[dict]:
    parts: list[dict] = []
    for number, (memory, jpeg) in enumerate(frames, start=1):
        parts.append({"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(jpeg).decode()}})
        parts.append({"text": _frame_label(number, memory)})
    return parts


def _frame_label(number: int, memory: Memory) -> str:
    when = datetime.fromtimestamp(memory.stamp).strftime("%Y-%m-%d %H:%M")
    return (
        f"Frame {number} — recorded {when}, from map position x={memory.x:.2f}m y={memory.y:.2f}m "
        f"heading={math.degrees(memory.theta):.0f}°"
    )


def _question(query: str) -> str:
    return f'The robot needs: "{query}". Which frame best serves this?'


def _parse_verdict(response: dict) -> tuple[bool, int, str] | None:
    try:
        parts = response["candidates"][0]["content"]["parts"]
        text = next(part["text"] for part in parts if part.get("text") and not part.get("thought"))
        data = json.loads(text)
        return bool(data["found"]), int(data["frame"]), str(data.get("explanation", ""))
    except (KeyError, IndexError, TypeError, ValueError, StopIteration):
        return None


def _age_text(seconds: float) -> str:
    if seconds < 90:
        return f"{max(round(seconds), 0)}s"
    if seconds < 90 * 60:
        return f"{round(seconds / 60)}min"
    if seconds < 36 * 3600:
        return f"{round(seconds / 3600)}h"
    return f"{round(seconds / 86400)}d"
