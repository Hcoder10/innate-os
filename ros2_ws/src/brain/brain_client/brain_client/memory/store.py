# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Persistent per-map spatial memory: a JSON index plus one JPEG per memory.

Lives under ``data/spatial_memory/<map>/`` beside the maps it describes and
survives restarts. Memories only mean anything in the coordinate frame of the
map they were recorded on, so each map gets its own directory, and the index
carries a fingerprint of the map file — re-mapping under the same name yields
a new frame, and the stale memories are wiped rather than trusted.

Thread contract: mutations come from the recorder's timer (executor thread);
:meth:`snapshot` serves readers on the agent loop and search threads. Every
index access takes the store lock; image files are read without it, so a
concurrently evicted file reads as missing — callers tolerate that.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Lock

_INDEX_VERSION = 1


@dataclass(frozen=True)
class Memory:
    """One remembered viewpoint: where the robot stood and when it looked."""

    id: int
    x: float
    y: float
    theta: float
    stamp: float  # epoch seconds at capture


@dataclass(frozen=True)
class MemorySnapshot:
    """An immutable view of the store for readers on other threads."""

    map_name: str | None
    revision: int
    memories: tuple[Memory, ...]
    # Same-name remaps reset memory ids, so name equality alone cannot prove
    # two snapshots share a coordinate frame — the fingerprint can.
    fingerprint: str = ""


class MemoryStore:
    def __init__(self, data_dir: Path):
        self._maps_dir = data_dir / "maps"
        self._root = data_dir / "spatial_memory"
        self._lock = Lock()
        self._map_name: str | None = None
        self._dir: Path | None = None
        self._memories: list[Memory] = []
        self._next_id = 1
        self._fingerprint = ""
        self._stat_sig: tuple[int, int] | None = None
        self._revision = 0
        self.last_change_monotonic = 0.0

    def switch_map(self, map_name: str | None) -> None:
        """Point the store at a map's memories, loading them from disk.

        Called every tick: same name + unchanged file stat is a cheap no-op,
        and hashing runs only when the name or the map file changes — so
        re-mapping over the active map's own name is caught within a tick.
        """
        stat_sig = _map_stat(self._maps_dir, map_name) if map_name else None
        if map_name == self._map_name and stat_sig == self._stat_sig:
            return
        fingerprint = _map_fingerprint(self._maps_dir, map_name) if map_name else ""
        if map_name is not None and not fingerprint:
            # The map file is unreadable this tick (being rewritten, transient
            # IO error). "Can't verify" must never wipe — retry next tick.
            return
        with self._lock:
            self._stat_sig = stat_sig
            if map_name == self._map_name and fingerprint == self._fingerprint:
                return  # the file was touched, not re-made
            self._map_name = map_name
            self._dir = self._root / Path(map_name).stem if map_name else None
            self._memories = []
            self._next_id = 1
            self._fingerprint = fingerprint
            self._revision += 1
            self.last_change_monotonic = time.monotonic()
            if self._dir is not None:
                self._load_locked()

    def snapshot(self) -> MemorySnapshot:
        with self._lock:
            return MemorySnapshot(self._map_name, self._revision, tuple(self._memories), self._fingerprint)

    @property
    def fingerprint(self) -> str:
        """Identity of the loaded map's content — changes exactly when the memories wipe."""
        with self._lock:
            return self._fingerprint

    def positions(self) -> list[dict]:
        """The webapp mirror payload: one ``{id, x, y, theta, stamp}`` per memory,
        rounded to display precision — full floats bloat the JSON by half."""
        with self._lock:
            return [
                {
                    "id": m.id,
                    "x": round(m.x, 3),
                    "y": round(m.y, 3),
                    "theta": round(m.theta, 4),
                    "stamp": round(m.stamp, 1),
                }
                for m in self._memories
            ]

    def image_path(self, memory_id: int) -> Path | None:
        with self._lock:
            return self._dir / f"{memory_id}.jpg" if self._dir is not None else None

    def files_index_path(self) -> Path | None:
        """Where the current map's server-side-upload registry lives (brain/frame_files.py)."""
        with self._lock:
            return self._dir / "files.json" if self._dir is not None else None

    def add(self, x: float, y: float, theta: float, stamp: float, jpeg: bytes) -> Memory | None:
        """Record a new memory; None when no map is loaded."""
        with self._lock:
            if self._dir is None:
                return None
            memory = Memory(self._next_id, x, y, theta, stamp)
            self._next_id += 1
            self._write_image_locked(memory.id, jpeg)
            self._memories.append(memory)
            self._commit_locked()
            return memory

    def replace(self, old: Memory, x: float, y: float, theta: float, stamp: float, jpeg: bytes) -> None:
        """Overwrite a memory in place — same slot, fresh view of the same spot."""
        with self._lock:
            if self._dir is None or all(m.id != old.id for m in self._memories):
                return
            memory = Memory(old.id, x, y, theta, stamp)
            self._write_image_locked(memory.id, jpeg)
            self._memories = [memory if m.id == old.id else m for m in self._memories]
            self._commit_locked()

    def clear(self) -> int:
        """Forget every memory on the current map — images, index, and upload
        registry — returning how many were forgotten."""
        with self._lock:
            if self._dir is None or not self._memories:
                return 0
            cleared = len(self._memories)
            self._wipe_locked()
            self._commit_locked()
            return cleared

    def evict(self, memory: Memory) -> None:
        self.forget(memory.id)

    def forget(self, memory_id: int, fingerprint: str = "") -> Memory | None:
        """Evict one memory by id, returning it — None when the id is unknown
        or the caller's fingerprint is stale (a re-map restarts ids, so a stale
        client must not delete the new map's memories; empty skips the check).
        Prefix-matched: the positions payload publishes a truncated digest."""
        with self._lock:
            if self._dir is None or (fingerprint and not self._fingerprint.startswith(fingerprint)):
                return None
            memory = next((m for m in self._memories if m.id == memory_id), None)
            if memory is None:
                return None
            self._memories = [m for m in self._memories if m.id != memory_id]
            (self._dir / f"{memory_id}.jpg").unlink(missing_ok=True)
            self._commit_locked()
            return memory

    # --- locked internals ---
    def _load_locked(self) -> None:
        assert self._dir is not None
        try:
            index = json.loads((self._dir / "index.json").read_text())
            fresh = (
                isinstance(index, dict)
                and index.get("version") == _INDEX_VERSION
                and index.get("fingerprint") == self._fingerprint
            )
            if fresh:
                self._memories = [Memory(**entry) for entry in index.get("memories", [])]
                self._next_id = int(index.get("next_id", 1))
                return
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass  # a wrong-shaped index is as stale as a wrong fingerprint
        self._wipe_locked()

    def _wipe_locked(self) -> None:
        """The map was re-made (or the index is unreadable): the coordinates lie."""
        assert self._dir is not None
        if self._dir.is_dir():
            for stale in self._dir.glob("*.jpg*"):  # images and any crash-orphaned .jpg.tmp
                stale.unlink(missing_ok=True)
            (self._dir / "index.json").unlink(missing_ok=True)
            (self._dir / "files.json").unlink(missing_ok=True)
        self._memories = []
        self._next_id = 1

    def _write_image_locked(self, memory_id: int, jpeg: bytes) -> None:
        # tmp + replace like the index: the proxy and upload threads read these
        # files without the lock and must never see a torn frame.
        assert self._dir is not None
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._dir / f"{memory_id}.jpg.tmp"
        tmp.write_bytes(jpeg)
        os.replace(tmp, self._dir / f"{memory_id}.jpg")

    def _commit_locked(self) -> None:
        assert self._dir is not None
        index = {
            "version": _INDEX_VERSION,
            "map": self._map_name,
            "fingerprint": self._fingerprint,
            "next_id": self._next_id,
            "memories": [asdict(m) for m in self._memories],
        }
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._dir / "index.json.tmp"
        tmp.write_text(json.dumps(index))
        os.replace(tmp, self._dir / "index.json")
        self._revision += 1
        self.last_change_monotonic = time.monotonic()


def _map_source(maps_dir: Path, map_name: str) -> Path:
    pgm = maps_dir / (Path(map_name).stem + ".pgm")
    return pgm if pgm.exists() else maps_dir / map_name


def _map_stat(maps_dir: Path, map_name: str) -> tuple[int, int] | None:
    try:
        stat = _map_source(maps_dir, map_name).stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _map_fingerprint(maps_dir: Path, map_name: str) -> str:
    """Identity of the map's content, not its name — a re-mapped room must not match."""
    try:
        return hashlib.sha256(_map_source(maps_dir, map_name).read_bytes()).hexdigest()
    except OSError:
        return ""
